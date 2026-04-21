#!/usr/bin/env python3
"""
ai-worker Discord 봇 (통합 + 인증 시스템)
- !search      : 웹 검색 + AI 요약
- !build       : AI 코드 생성 → commit/push (관리자 전용)
- !status      : 서버 상태 (CPU/RAM/GPU)
- !ci_status   : GitHub CI 상태
- !weather_sync: 날씨 정보 동기화
- !read_code   : 소스 코드 읽기
- !write_code  : 소스 코드 쓰기 (관리자 전용)
- !restart_telegram_bot : 텔레그램 봇 재시작 (관리자 전용)
- 일반 메시지    : 3-Agent 라우팅 자동 검색/답변
"""
import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import discord
import yaml
from discord.ext import commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

_agent_dir = Path(__file__).parent.resolve()
if str(_agent_dir) not in sys.path:
    sys.path.append(str(_agent_dir))

from core.bot_config import (
    CFG,
    DATA_DIR,
    OLLAMA_URL,
    OLLAMA_MODEL,
    LLAMA_CPP_URL,
    LLAMA_CPP_MODEL,
    MODEL_BACKEND,
)
from services.search_cache import search_naver as _search_naver

DISCORD_BOT_TOKEN = (
    CFG.get("discord", {}).get("bot_token")
    or os.environ.get("DISCORD_BOT_TOKEN", "")
)

# ── 인증 설정 ──────────────────────────────────────────────────────────────────
DISCORD_ADMIN_ID: int = int(
    CFG.get("discord", {}).get("admin_id")
    or os.environ.get("DISCORD_ADMIN_ID", "0")
)

_raw_allowed = (
    CFG.get("discord", {}).get("allowed_user_ids")
    or os.environ.get("DISCORD_ALLOWED_USER_IDS", "")
)
ALLOWED_USER_IDS: set[int] = {DISCORD_ADMIN_ID} if DISCORD_ADMIN_ID else set()
for _id in str(_raw_allowed).split(","):
    _id = _id.strip()
    if _id:
        ALLOWED_USER_IDS.add(int(_id))

_auth_fail_notified: dict[int, str] = {}


def _auth(ctx) -> bool:
    """허용된 사용자인지 확인. 미설정 시 모든 사용자 허용(하위 호환)."""
    if not ALLOWED_USER_IDS:
        return True
    return ctx.author.id in ALLOWED_USER_IDS


def _admin(ctx) -> bool:
    """관리자인지 확인."""
    if not DISCORD_ADMIN_ID:
        return False
    return ctx.author.id == DISCORD_ADMIN_ID


def _auth_msg(ctx) -> bool:
    """일반 메시지용 인증 (on_message에서 사용)."""
    if not ALLOWED_USER_IDS:
        return True
    return ctx.author.id in ALLOWED_USER_IDS


# ── 유틸리티 ───────────────────────────────────────────────────────────────────
def get_user_dir(user_id: int) -> Path:
    user_dir = DATA_DIR / f"discord_{user_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir

def save_chat(user_id: int, role: str, content: str):
    user_dir = get_user_dir(user_id)
    history_file = user_dir / "chat_history.jsonl"
    with open(history_file, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": role, "content": content, "ts": datetime.now().isoformat()}, ensure_ascii=False) + "\n")

def load_context(user_id: int, limit: int = 30) -> str:
    user_dir = get_user_dir(user_id)
    history_file = user_dir / "chat_history.jsonl"
    summary_file = user_dir / "summary.txt"
    context_parts = []
    if summary_file.exists():
        summary = summary_file.read_text(encoding="utf-8")
        context_parts.append(f"### [이전 대화 요약]\n{summary}\n")
    if history_file.exists():
        with open(history_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            recent_lines = lines[-limit:]
            history_text = ""
            for line in recent_lines:
                data = json.loads(line)
                role_name = "사용자" if data["role"] == "user" else "AI"
                history_text += f"{role_name}: {data['content']}\n"
            if history_text:
                context_parts.append(f"### [최근 대화 내역]\n{history_text}")
    return "\n".join(context_parts)

def _ask_ollama_raw(prompt: str, system_prompt: str = "", max_tokens: int = 8192, timeout: int = 300) -> str:
    import urllib.request
    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": full_prompt,
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "num_ctx": 16384,
        },
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["response"]


def _ask_llama_cpp(prompt: str, system_prompt: str = "", max_tokens: int = 8192, timeout: int = 300) -> str:
    import urllib.request
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    body: dict = {"messages": messages, "max_tokens": max_tokens}
    if LLAMA_CPP_MODEL:
        body["model"] = LLAMA_CPP_MODEL
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{LLAMA_CPP_URL}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = json.loads(resp.read())["choices"][0]["message"]["content"]
    return re.sub(r'<\|[^|]*\|>', '', text).strip()


def ask_ollama(prompt: str, system_prompt: str = "", max_tokens: int = 8192, timeout: int = 300) -> str:
    if MODEL_BACKEND == "llama-cpp":
        return _ask_llama_cpp(prompt, system_prompt, max_tokens, timeout)
    return _ask_ollama_raw(prompt, system_prompt, max_tokens, timeout)

async def check_and_summarize(user_id: int):
    user_dir = get_user_dir(user_id)
    history_file = user_dir / "chat_history.jsonl"
    if not history_file.exists(): return
    with open(history_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) > 30:
        log.info(f"User {user_id}: 히스토리 요약 시작...")
        to_summarize = lines[:20]
        remaining = lines[20:]
        old_text = "".join(f"{json.loads(l)['role']}: {json.loads(l)['content']}\n" for l in to_summarize)
        summary_file = user_dir / "summary.txt"
        existing_summary = summary_file.read_text(encoding="utf-8") if summary_file.exists() else "없음"
        prompt = (f"기존 요약본과 새 대화 내용을 합쳐 최신 상태로 요약해줘.\n\n[기존]\n{existing_summary}\n\n[새 대화]\n{old_text}")
        loop = asyncio.get_event_loop()
        new_summary = await loop.run_in_executor(None, lambda: ask_ollama(prompt))
        summary_file.write_text(new_summary, encoding="utf-8")
        with open(history_file, "w", encoding="utf-8") as f: f.writelines(remaining)

def _strip_markup(text: str) -> str:
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'[`*#_]', '', text)
    return text.strip()

# ── 안전한 경로 검증 ──────────────────────────────────────────────────────────
_AGENT_DIR = Path(__file__).parent.resolve()

def _safe_agent_path(filename: str) -> Path | None:
    """에이전트 디렉토리 내 파일만 허용. 경로 탈출/심볼릭 링크 차단."""
    if ".." in filename or "/" in filename or "\\" in filename or filename.startswith("."):
        return None
    target = _AGENT_DIR / filename
    # resolve()로 심볼릭 링크 해소 후 실제 경로가 에이전트 디렉토리 안인지 확인
    try:
        resolved = target.resolve()
        if not resolved.is_relative_to(_AGENT_DIR):
            return None
        if not resolved.exists() or not resolved.is_file():
            return None
        return resolved
    except (OSError, ValueError):
        return None


# ── 봇 클래스 ──────────────────────────────────────────────────────────────────
class AIWorkerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def on_ready(self):
        auth_info = f"admin={DISCORD_ADMIN_ID}, allowed={ALLOWED_USER_IDS}" if DISCORD_ADMIN_ID else "인증 미설정 (모든 사용자 허용)"
        log.info(f"봇 로그인 완료: {self.user.name} ({self.user.id}) [{auth_info}]")

    async def on_message(self, message):
        if message.author.bot: return

        # 1. 명령어 처리 우선
        if message.content.startswith(self.command_prefix):
            await self.process_commands(message)
            return

        # 2. 인증 확인
        if not _auth_msg(message):
            return

        text = message.content.strip()

        # 3. 파일명 단독 입력 시 자동 읽기 (안전한 경로 검증 사용)
        if " " not in text and "\n" not in text:
            safe_path = _safe_agent_path(text)
            if safe_path:
                content = safe_path.read_text(encoding="utf-8")
                import io
                file_obj = io.BytesIO(content.encode('utf-8'))
                discord_file = discord.File(fp=file_obj, filename=safe_path.name)
                await message.reply(f"📄 파일명 자동 감지 ({text}):", file=discord_file)
                return

        # 4. 일반 메시지 (3-Agent AI 채팅)
        user_id = message.author.id
        save_chat(user_id, "user", text)

        async with message.channel.typing():
            chat_context = load_context(user_id)

            # --- 로컬 파일 언급 감지 및 컨텍스트 자동 주입 ---
            local_files_context = ""
            for fname in os.listdir(Path(__file__).parent):
                if fname.endswith(".py") and fname in text:
                    safe_path = _safe_agent_path(fname)
                    if safe_path:
                        try:
                            f_content = safe_path.read_text(encoding="utf-8")
                            local_files_context += f"### [참고 자료 - 로컬 파일: {fname}]\n```python\n{f_content}\n```\n\n"
                        except:
                            pass

            loop = asyncio.get_event_loop()

            try:
                # Agent 1: Planner
                planner_prompt = (f"JSON 형식으로만 답해. 검색 필요 여부를 판단해.\n질문: {text}\n출력 예: {{\"action\": \"direct\"}} 또는 {{\"action\": \"search\", \"keyword\": \"...\"}}")
                if local_files_context:
                    planner_prompt += "\n참고: 질문에 언급된 코드를 이미 시스템이 제공했으므로 소스 코드 확인 목적의 웹 검색은 불필요합니다. 'direct' 액션을 선택하세요."

                plan_response = await loop.run_in_executor(None, lambda: ask_ollama(planner_prompt, max_tokens=4096))

                action = "direct"
                keyword = None
                match = re.search(r'\{.*\}', plan_response, re.DOTALL)
                if match:
                    try:
                        plan_json = json.loads(match.group(0))
                        action = plan_json.get("action", "direct")
                        keyword = plan_json.get("keyword")
                    except: pass

                if action == "search" and keyword:
                    results = await loop.run_in_executor(None, _search_naver, keyword)
                    eval_prompt = (f"질문: {text}\n검색결과: {results[:3000]}\n위 정보를 바탕으로 자세하고 친절하게 답변해줘. 마크다운(bold, code blocks 등)을 적절히 활용해 가독성을 높여줘.")
                    if local_files_context:
                        eval_prompt = local_files_context + eval_prompt
                    response = await loop.run_in_executor(None, lambda: ask_ollama(eval_prompt, max_tokens=8192))
                else:
                    system_prompt = "유능한 AI 어시스턴트로서 사용자 질문에 친절하고 상세하게 답해줘. 마크다운(bold, code blocks 등)을 적절히 활용해 가독성을 높여줘."
                    full_text = f"{chat_context}\n\n사용자: {text}"
                    if local_files_context:
                        full_text = f"{local_files_context}{full_text}"
                    response = await loop.run_in_executor(None, lambda: ask_ollama(full_text, system_prompt, max_tokens=8192))

                cleaned = response.strip()
                save_chat(user_id, "assistant", cleaned)
                await check_and_summarize(user_id)

                # 2000자 초과 시 디스코드 제한 우회 (분할 전송)
                if len(cleaned) > 2000:
                    for i in range(0, len(cleaned), 1900):
                        await message.reply(cleaned[i:i+1900])
                else:
                    await message.reply(cleaned)

            except Exception as e:
                log.error(f"메시지 처리 오류: {e}")
                await message.reply(f"❌ 오류 발생: {e}")

bot = AIWorkerBot()

# ── 명령어 ─────────────────────────────────────────────────────────────────────
@bot.command(name="search")
async def cmd_search(ctx, *, query: str):
    if not _auth(ctx):
        await ctx.send("🔒 권한이 없습니다.")
        return
    async with ctx.typing():
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, _search_naver, query)
        prompt = f"다음 검색 결과를 상세히 요약해줘 (2000자 이내). 마크다운을 활용해 가독성 있게 작성해: {results[:3000]}"
        summary = await loop.run_in_executor(None, lambda: ask_ollama(prompt, max_tokens=8192))
        msg = f"🔍 **검색 결과 요약: {query}**\n\n{summary.strip()}\n\n*(상세 정보 생략됨)*"
        if len(msg) > 2000:
            for i in range(0, len(msg), 1900):
                await ctx.send(msg[i:i+1900])
        else:
            await ctx.send(msg)

@bot.command(name="build")
async def cmd_build(ctx, *, request: str):
    if not _admin(ctx):
        await ctx.send("🔒 관리자 전용 명령입니다.")
        return
    async with ctx.typing():
        def run_handler():
            sys.path.insert(0, str(Path(__file__).parent / "scripts"))
            from request_handler import handle_request
            return handle_request(request)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_handler)
        res_text = _strip_markup(result)
        if len(res_text) > 2000:
            for i in range(0, len(res_text), 1900):
                await ctx.send(res_text[i:i+1900])
        else:
            await ctx.send(res_text)

@bot.command(name="status")
async def cmd_status(ctx):
    if not _auth(ctx):
        await ctx.send("🔒 권한이 없습니다.")
        return
    try:
        import psutil, pynvml
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        pynvml.nvmlInit()
        gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
        gpu_util = pynvml.nvmlDeviceGetUtilizationRates(gpu).gpu
        pynvml.nvmlShutdown()
        await ctx.send(f"📊 서버 상태\nCPU: {cpu}%\nRAM: {mem.percent}%\nGPU: {gpu_util}%")
    except Exception as e:
        await ctx.send(f"❌ 상태 확인 실패: {e}")

@bot.command(name="ci_status")
async def cmd_ci_status(ctx):
    if not _auth(ctx):
        await ctx.send("🔒 권한이 없습니다.")
        return
    try:
        from github import Github
        token = CFG.get("github", {}).get("token") or os.environ.get("GITHUB_TOKEN", "")
        repo_name = CFG.get("github", {}).get("repo", "")
        gh = Github(token)
        repo = gh.get_repo(repo_name)
        runs = list(repo.get_workflow_runs(branch="master")[:3])
        lines = ["📊 최근 CI 상태:"]
        for r in runs:
            icon = "✅" if r.conclusion == "success" else "❌"
            lines.append(f"{icon} #{r.id} ({r.conclusion or r.status})")
        await ctx.send("\n".join(lines))
    except Exception as e:
        await ctx.send(f"❌ CI 상태 확인 실패: {e}")

@bot.command(name="weather_sync")
async def cmd_weather_sync(ctx):
    if not _auth(ctx):
        await ctx.send("🔒 권한이 없습니다.")
        return
    from services.weather_service import sync_all
    await ctx.send("🌦️ 날씨 정보 동기화 중...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, sync_all)
    await ctx.send("✅ 동기화 완료!")

@bot.command(name="read_code")
async def cmd_read_code(ctx, target: str = "telegram_bot.py"):
    if not _auth(ctx):
        await ctx.send("🔒 권한이 없습니다.")
        return
    safe_path = _safe_agent_path(target)
    if not safe_path:
        await ctx.send("❌ 해당 파일에는 접근할 수 없습니다.")
        return

    content = safe_path.read_text(encoding="utf-8")
    import io
    file_obj = io.BytesIO(content.encode('utf-8'))
    discord_file = discord.File(fp=file_obj, filename=target)
    await ctx.send(f"📄 '{target}' 소스 코드:", file=discord_file)


@bot.command(name="write_code")
async def cmd_write_code(ctx, target: str = ""):
    if not _admin(ctx):
        await ctx.send("🔒 관리자 전용 명령입니다.")
        return

    if not target:
        await ctx.send("❌ 사용법: `!write_code <파일명>`\n예: `!write_code telegram_bot.py`")
        return

    # 안전한 경로 검증 (.py 파일만 허용)
    if not target.endswith(".py"):
        await ctx.send("❌ .py 파일만 쓸 수 있습니다.")
        return
    if ".." in target or "/" in target or "\\" in target or target.startswith("."):
        await ctx.send("❌ 해당 파일에는 접근할 수 없습니다.")
        return

    target_path = _AGENT_DIR / target

    code_to_write = ""
    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]
        code_to_write = (await attachment.read()).decode("utf-8")
    else:
        match = re.search(r'```(?:python|py)?\n(.*?)```', ctx.message.content, re.DOTALL)
        if match:
            code_to_write = match.group(1)
        else:
            await ctx.send("❌ 첨부 파일(.py)이나 코드 블록(```python ... ```)을 제공해주세요.")
            return

    import shutil
    if target_path.exists():
        bak_path = target_path.with_name(target_path.name + ".bak")
        shutil.copy2(target_path, bak_path)

    target_path.write_text(code_to_write, encoding="utf-8")
    log.info(f"[AUDIT] write_code: user={ctx.author.id} ({ctx.author.name}), file={target}")
    await ctx.send(f"✅ '{target}' 파일이 성공적으로 덮어씌워졌습니다!\n(이전 버전은 {target}.bak 로 백업되었습니다.)")


@bot.command(name="restart_telegram_bot")
async def cmd_restart_telegram_bot(ctx):
    if not _admin(ctx):
        await ctx.send("🔒 관리자 전용 명령입니다.")
        return
    await ctx.send("🔄 `telegram-bot.service` 재시작 중...")
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "telegram-bot"],
            check=True, capture_output=True, text=True
        )
        log.info(f"[AUDIT] restart_telegram_bot: user={ctx.author.id} ({ctx.author.name})")
        await ctx.send("✅ 텔레그램 봇 재시작이 완료되었습니다!")
    except subprocess.CalledProcessError as e:
        await ctx.send(f"❌ 서비스 재시작 실패: {e}\n```\n{e.stderr}\n```")


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        log.error("Discord bot token이 설정되지 않았습니다.")
    else:
        if not DISCORD_ADMIN_ID:
            log.warning("⚠️ DISCORD_ADMIN_ID 미설정 — 관리자 명령 사용 불가. .env에 DISCORD_ADMIN_ID를 설정하세요.")
        if not ALLOWED_USER_IDS:
            log.warning("⚠️ DISCORD_ALLOWED_USER_IDS 미설정 — 모든 사용자 접근 허용 상태입니다.")
        bot.run(DISCORD_BOT_TOKEN)
