#!/usr/bin/env python3
"""
bot_commands.py — 모든 /명령어 핸들러 + 날씨 동기화 루프
의존성: bot_config, bot_utils
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from bot_config import CFG, RUNNER_PATTERN, TELEGRAM_CHAT_ID, RAG_ENABLED, TELEGRAM_FORMAT_RULES
from bot_utils import (
    _admin,
    _auth,
    _escape_html,
    _send_long,
    _strip_tags,
    ask_ollama,
    check_and_summarize,
    save_chat,
    SEARCH_SYSTEM_PROMPT,
    _clear_history,
    _load_history,
    _save_history,
    _truncate_messages,
)
from search_cache import search_naver as _search_naver
import rag_manager

log = logging.getLogger(__name__)


# ── 사용자 관리 ───────────────────────────────────────────────────────────────

async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """관리자가 사용자 chat_id를 허용 목록에 추가"""
    from bot_config import ALLOWED_CHAT_IDS
    if not _admin(update):
        return
    if not context.args:
        await update.message.reply_text("사용법: /allow <chat_id>")
        return
    try:
        new_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id는 숫자여야 합니다.")
        return
    if new_id in ALLOWED_CHAT_IDS:
        await update.message.reply_text(f"{new_id}는 이미 허용된 사용자입니다.")
        return
    ALLOWED_CHAT_IDS.add(new_id)
    env_path = Path(__file__).parent.parent / ".env"
    env_lines = env_path.read_text().splitlines() if env_path.exists() else []
    new_lines = []
    found = False
    ids_str = ",".join(str(i) for i in ALLOWED_CHAT_IDS)
    for line in env_lines:
        if line.startswith("TELEGRAM_ALLOWED_CHAT_IDS="):
            new_lines.append(f"TELEGRAM_ALLOWED_CHAT_IDS={ids_str}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"TELEGRAM_ALLOWED_CHAT_IDS={ids_str}")
    env_path.write_text("\n".join(new_lines) + "\n")
    await update.message.reply_text(f"✅ {new_id} 허용 완료. 현재 허용 목록: {ALLOWED_CHAT_IDS}")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """관리자가 사용자 chat_id를 허용 목록에서 제거"""
    from bot_config import ALLOWED_CHAT_IDS
    if not _admin(update):
        return
    if not context.args:
        await update.message.reply_text("사용법: /deny <chat_id>")
        return
    try:
        rm_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id는 숫자여야 합니다.")
        return
    if rm_id == TELEGRAM_CHAT_ID:
        await update.message.reply_text("자기 자신은 추방할 수 없습니다.")
        return
    if rm_id not in ALLOWED_CHAT_IDS:
        await update.message.reply_text(f"{rm_id}는 허용 목록에 없습니다.")
        return
    ALLOWED_CHAT_IDS.discard(rm_id)
    env_path = Path(__file__).parent.parent / ".env"
    env_lines = env_path.read_text().splitlines() if env_path.exists() else []
    new_lines = []
    found = False
    ids_str = ",".join(str(i) for i in ALLOWED_CHAT_IDS)
    for line in env_lines:
        if line.startswith("TELEGRAM_ALLOWED_CHAT_IDS="):
            new_lines.append(f"TELEGRAM_ALLOWED_CHAT_IDS={ids_str}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"TELEGRAM_ALLOWED_CHAT_IDS={ids_str}")
    env_path.write_text("\n".join(new_lines) + "\n")
    await update.message.reply_text(f"🚫 {rm_id} 추방 완료. 현재 허용 목록: {ALLOWED_CHAT_IDS}")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """허용된 사용자 목록 확인"""
    from bot_config import ALLOWED_CHAT_IDS
    if not _admin(update):
        return
    lines = ["📋 허용된 사용자 목록\n"]
    for cid in sorted(ALLOWED_CHAT_IDS):
        tag = " (관리자)" if cid == TELEGRAM_CHAT_ID else ""
        lines.append(f"  {cid}{tag}")
    await update.message.reply_text("\n".join(lines))


# ── RAG (지식 검색) ─────────────────────────────────────────────────────────────

async def cmd_index(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """지식 창고(/knowledge) 및 코드를 인덱싱합니다."""
    if not _admin(update):
        return
    if not RAG_ENABLED:
        await update.message.reply_text("❌ RAG 기능이 비활성화되어 있습니다. config.yaml을 확인하세요.")
        return

    msg = await update.message.reply_text("📚 지식 인덱싱 수동 실행 중... (백그라운드)")
    
    loop = asyncio.get_event_loop()
    try:
        # /index 명령은 소스 코드 포함 전체 인덱싱 수행
        result = await loop.run_in_executor(None, lambda: rag_manager.index_knowledge(include_code=True))
        await msg.edit_text(result)
    except Exception as e:
        await msg.edit_text(f"❌ 인덱싱 중 오류 발생: {e}")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """지식 기반 질문 답변 (/ask <질문>)"""
    from bot_config import TELEGRAM_FORMAT_RULES
    if not _auth(update):
        return
    if not RAG_ENABLED:
        await update.message.reply_text("❌ RAG 기능이 비활성화되어 있습니다.")
        return

    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("사용법: /ask &lt;질문&gt;", parse_mode="HTML")
        return

    status_msg = await update.message.reply_text("🧠 로컬 지식 창고 검색 중...")
    user_id = update.effective_user.id
    save_chat(user_id, "user", f"[지식 질문] {query}")

    loop = asyncio.get_event_loop()
    try:
        # 1. RAG 검색
        knowledge_context = await loop.run_in_executor(None, lambda: rag_manager.query_knowledge(query))
        
        # 2. AI 답변 생성
        await status_msg.edit_text("💡 검색된 정보를 바탕으로 답변 작성 중...")
        prompt = (
            f"사용자 질문: {query}\n\n"
            f"{knowledge_context}\n"
            f"위 지식 내용을 참고해서 답변해줘. 만약 지식 내용에 관련 정보가 없다면 너의 기존 지식으로 답하되, '문서에서 찾을 수 없습니다'라고 언급해줘.\n\n"
            f"{TELEGRAM_FORMAT_RULES}"
        )
        response = await loop.run_in_executor(None, lambda: ask_ollama(prompt))
        
        cleaned = _strip_tags(response.strip())
        await status_msg.delete()
        await _send_long(update, cleaned)
        save_chat(user_id, "assistant", cleaned)
        await check_and_summarize(user_id)
        
    except Exception as e:
        await status_msg.edit_text(f"❌ 지식 답변 중 오류: {e}")


# ── CI / 루프 상태 ─────────────────────────────────────────────────────────────

async def cmd_ci_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return

    loop = asyncio.get_event_loop()

    def check_ci():
        from github import Github
        token = CFG.get("github", {}).get("token") or os.environ.get("GITHUB_TOKEN", "")
        repo_name = CFG.get("github", {}).get("repo", "")
        if not token or not repo_name:
            return "❌ GitHub 설정이 없습니다."
        gh = Github(token)
        repo = gh.get_repo(repo_name)
        runs = list(repo.get_workflow_runs(branch="master")[:5])
        if not runs:
            return "최근 CI 실행 없음"
        lines = ["📊 <b>최근 CI 실행 (5개)</b>\n"]
        for r in runs:
            status_icon = "✅" if r.conclusion == "success" else "❌" if r.conclusion == "failure" else "⏳"
            lines.append(
                f"{status_icon} #{r.id} — {r.conclusion or r.status}\n"
                f"   {r.created_at.strftime('%m-%d %H:%M')}"
            )
        return "\n".join(lines)

    try:
        result = await loop.run_in_executor(None, check_ci)
    except Exception as e:
        result = f"❌ CI 상태 확인 실패: {e}"
    await update.message.reply_text(result, parse_mode="HTML")


async def cmd_loop_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return

    loop = asyncio.get_event_loop()

    def check_loop():
        result = subprocess.run(
            ["pgrep", "-af", "loop.py"],
            capture_output=True, text=True, timeout=5,
        )
        processes = [
            line for line in result.stdout.strip().splitlines()
            if "loop.py" in line and "pgrep" not in line
        ]
        if processes:
            return "✅ AI 루프 실행 중\n\n" + "\n".join(processes)
        return "⚠️ AI 루프가 실행되고 있지 않습니다."

    try:
        result = await loop.run_in_executor(None, check_loop)
    except Exception as e:
        result = f"❌ 상태 확인 실패: {e}"
    await update.message.reply_text(result, parse_mode="HTML")


# ── 시스템 명령 ───────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return

    loop = asyncio.get_event_loop()

    def get_stats():
        import psutil
        import pynvml

        cpu_avg = psutil.cpu_percent(interval=1)
        cpu_temp = None
        try:
            temps = psutil.sensors_temperatures()
            for key in ("k10temp", "coretemp"):
                if key in temps:
                    entries = [e for e in temps[key] if e.label in ("Tctl", "Tccd1", "")]
                    if entries:
                        cpu_temp = max(e.current for e in entries)
                    break
        except Exception:
            pass

        mem = psutil.virtual_memory()

        pynvml.nvmlInit()
        gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
        gpu_util = pynvml.nvmlDeviceGetUtilizationRates(gpu)
        gpu_mem = pynvml.nvmlDeviceGetMemoryInfo(gpu)
        gpu_temp = pynvml.nvmlDeviceGetTemperature(gpu, pynvml.NVML_TEMPERATURE_GPU)
        gpu_power_mw = pynvml.nvmlDeviceGetPowerUsage(gpu)
        pynvml.nvmlShutdown()

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cpu_temp_str = f"{cpu_temp:.1f}°C" if cpu_temp else "N/A"

        return (
            f"📊 <b>서버 상태</b> ({ts})\n\n"
            f"<b>CPU</b>\n  평균: {cpu_avg:.1f}%  온도: {cpu_temp_str}\n\n"
            f"<b>RAM</b>\n  {mem.used / 1024**3:.1f}/{mem.total / 1024**3:.0f} GB  ({mem.percent:.1f}%)\n\n"
            f"<b>GPU</b>\n"
            f"  사용률: {gpu_util.gpu}%\n"
            f"  VRAM: {gpu_mem.used / 1024**3:.1f}/{gpu_mem.total / 1024**3:.0f} GB "
            f"({gpu_mem.used / gpu_mem.total * 100:.1f}%)\n"
            f"  온도: {gpu_temp}°C  전력: {gpu_power_mw / 1000:.0f}W"
        )

    try:
        result = await loop.run_in_executor(None, get_stats)
    except Exception as e:
        result = f"❌ 상태 확인 실패: {e}"
    await update.message.reply_text(result, parse_mode="HTML")


async def cmd_sh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    cmd = " ".join(context.args) if context.args else ""
    if not cmd:
        await update.message.reply_text("사용법: /sh &lt;명령어&gt;", parse_mode="HTML")
        return
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30),
        )
        output = result.stdout or ""
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        output = output.strip() or "(출력 없음)"
    except subprocess.TimeoutExpired:
        output = "⏰ 시간 초과 (30초)"
    except Exception as e:
        output = f"❌ 오류: {e}"
    await _send_long(update, output)


async def cmd_py(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    code = " ".join(context.args) if context.args else ""
    if not code:
        await update.message.reply_text("사용법: /py &lt;코드&gt;", parse_mode="HTML")
        return
    loop = asyncio.get_event_loop()
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            tmp = f.name
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(["python3", tmp], capture_output=True, text=True, timeout=30),
        )
        os.unlink(tmp)
        output = result.stdout or ""
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        output = output.strip() or "(출력 없음)"
    except subprocess.TimeoutExpired:
        output = "⏰ 시간 초과 (30초)"
    except Exception as e:
        output = f"❌ 오류: {e}"
    await _send_long(update, output)


async def cmd_ps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            ["ps", "aux", "--sort=-pcpu"], capture_output=True, text=True, timeout=10
        ),
    )
    lines = result.stdout.strip().splitlines()[:20]
    await update.message.reply_text("\n".join(lines))


async def cmd_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    args = context.args or []
    if len(args) < 2 or args[0] not in ("start", "stop", "restart", "status"):
        await update.message.reply_text("사용법: /service &lt;start|stop|restart|status&gt; &lt;서비스명&gt;", parse_mode="HTML")
        return
    action, svc = args[0], args[1]
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            ["systemctl", "--user", action, svc], capture_output=True, text=True, timeout=15
        ),
    )
    output = result.stdout or result.stderr or f"✅ {svc} {action} 완료"
    await update.message.reply_text(output.strip())


async def cmd_capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    path = "/tmp/monitor_screenshot.png"
    try:
        result = subprocess.run(
            ["scrot", path],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "DISPLAY": os.getenv("DISPLAY", ":0")},
        )
        if result.returncode != 0:
            await update.message.reply_text(f"❌ scrot 실패: {_escape_html(result.stderr[:200])}", parse_mode="HTML")
            return
        with open(path, "rb") as f:
            await update.message.reply_photo(f, caption=f"🖥️ {datetime.now().strftime('%H:%M:%S')}")
    except FileNotFoundError:
        await update.message.reply_text("❌ scrot이 설치되지 않았습니다. <code>sudo apt install scrot</code>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ 오류: {_escape_html(str(e))}", parse_mode="HTML")


async def cmd_restart_runner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    if not RUNNER_PATTERN:
        await update.message.reply_text("config.yaml에 github_runner.service_pattern이 설정되지 않았습니다.", parse_mode="HTML")
        return
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-units", "--plain", "--no-legend",
             "--type=service", RUNNER_PATTERN],
            capture_output=True, text=True, timeout=10,
        )
        services = [line.split()[0] for line in result.stdout.strip().splitlines() if line]
        if not services:
            await update.message.reply_text(f"패턴 <code>{_escape_html(RUNNER_PATTERN)}</code>에 해당하는 서비스가 없습니다.", parse_mode="HTML")
            return
        for svc in services:
            subprocess.run(["systemctl", "--user", "restart", svc], timeout=30)
        await update.message.reply_text(f"✅ 재시작 완료: {', '.join(services)}", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ 오류: {_escape_html(str(e))}", parse_mode="HTML")


# ── 날씨 동기화 ───────────────────────────────────────────────────────────────

async def weather_sync_loop():
    """매일 07:01, 13:01, 18:01에 날씨 정보를 동기화하는 루프"""
    from weather_service import sync_all

    log.info("날씨 동기화 백그라운드 태스크 시작")

    while True:
        now = datetime.now()
        if now.minute == 1 and now.hour in (7, 13, 18):
            log.info("정기 날씨 동기화 트리거됨 (현재 시각: %02d:%02d)", now.hour, now.minute)
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, sync_all)
            except Exception as e:
                log.error("정기 날씨 동기화 중 오류 발생: %s", e)
            await asyncio.sleep(60)
        await asyncio.sleep(30)


async def cmd_weather_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    from weather_service import sync_all
    await update.message.reply_text("🌦️ 안성 날씨 정보 동기화 시작...", parse_mode="HTML")
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, sync_all)
        await update.message.reply_text("✅ 날씨 정보 동기화 완료! 이제 캐시된 정보를 사용합니다.", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ 동기화 실패: {_escape_html(str(e))}", parse_mode="HTML")


async def cmd_reinforce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """00_Raw/ 파일을 Ollama로 요약해 10_Wiki/에 저장하고 GitHub 푸시 후 RAG 재인덱싱"""
    if not _auth(update):
        return
    msg = await update.message.reply_text("⚙️ p_reinforce 실행 중...", parse_mode="HTML")
    try:
        result = subprocess.run(
            ["python3", "/home/home/Jin_lifestyle/p_reinforce.py"],
            capture_output=True, text=True, timeout=300,
            cwd="/home/home/Jin_lifestyle"
        )
        output = result.stdout.strip() or result.stderr.strip() or "출력 없음"
        # 핵심 줄만 추출
        lines = [l for l in output.splitlines() if any(k in l for k in ["✨", "✅", "📄", "📦", "🚀", "❌", "📭"])]
        summary = "\n".join(lines) if lines else output[:500]
        await msg.edit_text(f"<pre>{_escape_html(summary)}</pre>", parse_mode="HTML")
    except subprocess.TimeoutExpired:
        await msg.edit_text("⏱️ 타임아웃 (5분 초과)", parse_mode="HTML")
        return
    except Exception as e:
        await msg.edit_text(f"❌ 오류: {_escape_html(str(e))}", parse_mode="HTML")
        return

    if RAG_ENABLED:
        rag_msg = await update.message.reply_text("📚 RAG 재인덱싱 중...", parse_mode="HTML")
        loop = asyncio.get_event_loop()
        try:
            rag_result = await loop.run_in_executor(None, lambda: rag_manager.index_knowledge())
            await rag_msg.edit_text(rag_result)
        except Exception as e:
            await rag_msg.edit_text(f"❌ RAG 인덱싱 오류: {_escape_html(str(e))}", parse_mode="HTML")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """대화 히스토리 초기화"""
    if not _auth(update):
        return
    chat_id = update.effective_chat.id
    _clear_history(chat_id)
    await update.message.reply_text("🗑️ 대화 기록이 초기화되었습니다.")
