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

from core.bot_config import CFG, RUNNER_PATTERN, TELEGRAM_CHAT_ID, RAG_ENABLED, TELEGRAM_FORMAT_RULES
from core.bot_utils import (
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
    _save_history,
    _truncate_messages,
)
from services.search_cache import search_naver as _search_naver
from services import rag_manager
# from services import wiki_navigator # 실종된 파일 임시 주석 처리

log = logging.getLogger(__name__)


# ── 사용자 관리 ───────────────────────────────────────────────────────────────

async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """관리자가 사용자 chat_id를 허용 목록에 추가"""
    from core.bot_config import ALLOWED_CHAT_IDS
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
    
    # Save dynamically to a JSON file instead of insecure .env mutation
    from core.bot_config import DATA_DIR
    import json
    allowed_json = DATA_DIR / "allowed_users.json"
    
    # We only save non-original (dynamic) IDs to the JSON to avoid duplication with config
    original_ids = {TELEGRAM_CHAT_ID} # Base admin
    # To keep it simple, we just save the whole current set minus the hardcoded admin
    dynamic_ids = [cid for cid in ALLOWED_CHAT_IDS if cid != TELEGRAM_CHAT_ID]
    
    allowed_json.write_text(json.dumps(dynamic_ids))
    
    await update.message.reply_text(f"✅ {new_id} 허용 완료. 현재 허용 목록: {ALLOWED_CHAT_IDS}")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """관리자가 사용자 chat_id를 허용 목록에서 제거"""
    from core.bot_config import ALLOWED_CHAT_IDS
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
    
    # Save dynamically to a JSON file instead of insecure .env mutation
    from core.bot_config import DATA_DIR
    import json
    allowed_json = DATA_DIR / "allowed_users.json"
    dynamic_ids = [cid for cid in ALLOWED_CHAT_IDS if cid != TELEGRAM_CHAT_ID]
    allowed_json.write_text(json.dumps(dynamic_ids))
    
    await update.message.reply_text(f"🚫 {rm_id} 추방 완료. 현재 허용 목록: {ALLOWED_CHAT_IDS}")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """허용된 사용자 목록 확인"""
    from core.bot_config import ALLOWED_CHAT_IDS
    if not _admin(update):
        return
    lines = ["📋 허용된 사용자 목록\n"]
    for cid in sorted(ALLOWED_CHAT_IDS):
        tag = " (관리자)" if cid == TELEGRAM_CHAT_ID else ""
        lines.append(f"  {cid}{tag}")
    await update.message.reply_text("\n".join(lines))


# ── /index ───────────────────────────────────────────────────────────────────

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
        result = await rag_manager.index_knowledge(include_code=True)
        await msg.edit_text(result)
    except Exception as e:
        await msg.edit_text(f"❌ 인덱싱 중 오류 발생: {e}")


async def cmd_roadmap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """AI 정보 자동화 파이프라인 3단계 로드맵 브리핑."""
    if not await _auth(update):
        return
    
    roadmap_text = (
        "<b>🗺 향후 로드맵: AI 정보 자동화 파이프라인</b>\n\n"
        "🥇 <b>1단계: 데이터 수집 엔진 구축 (MVP)</b>\n"
        "• 외부 AI 트렌드 소스(Meta, Anthropic 등) 전용 크롤러 구축\n"
        "• <code>00_Raw/AI_Curation/</code> 원본 저장 자동화\n\n"
        "🥈 <b>2단계: 지식 통합 및 구조화 (Core)</b>\n"
        "• 수집된 원본을 LLM으로 현진님 맞춤형 요약\n"
        "• <code>10_Wiki/synthesis/</code> 지식 구조화 통합\n\n"
        "🥉 <b>3단계: 배포 및 최적화 (Scaling)</b>\n"
        "• 텔레그램 일일 AI 브리핑 자동 알림 기능\n"
        "• 개인 도메인(전기기사 등) 연관 학습 엔진 확장\n\n"
        "<i>✅ 현재 Phase 1 안정화(Git/LLM 포트/폴백)가 완료되어 1단계 착수 준비가 끝났습니다.</i>"
    )
    await update.message.reply_text(roadmap_text, parse_mode="HTML")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """지식 기반 질문 답변 (/ask <질문>) — Agent 지식 순환 주기 적용"""
    from core.bot_config import TELEGRAM_FORMAT_RULES
    if not await _auth(update):
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
    await save_chat(user_id, "user", f"[지식 질문] {query}")

    try:
        from core.bot_utils import load_context, _truncate_messages
        context_data = await load_context(user_id)
        history_msgs = context_data["messages"]
        summary_text = context_data["summary"]

        from core.agent import get_agent
        agent = get_agent()

        # Agent: RAG 검색 → 없으면 외부 수집 → 구조화 → 저장
        knowledge_context = await agent.process_query(query)

        # graph.json으로 연관 페이지 추가 탐색
        graph_context = ""
        # try:
        #     query_keywords = query.split()
        #     index_data = wiki_navigator.parse_index()
        #     all_titles = [p["title"] for pages in index_data.values() for p in pages]
        #     matched = [t for t in all_titles if any(kw in t for kw in query_keywords)][:3]
        #     related_all: set[str] = set()
        #     for title in matched:
        #         related_all.update(wiki_navigator.get_related_pages(title, n=3))
        #     related_all -= set(matched)
        #     if related_all:
        #         graph_context = "\n[연관 위키 페이지]: " + ", ".join(f"[[{t}]]" for t in list(related_all)[:5])
        # except Exception:
        #     pass

        system_prompt = TELEGRAM_FORMAT_RULES
        if summary_text:
            system_prompt += f"\n\n[이전 대화 요약]\n{summary_text}"

        prompt = f"사용자 질문: {query}\n\n"
        if not knowledge_context:
            await status_msg.edit_text("💡 답변 작성 중...")
            prompt += "관련 문서를 찾을 수 없어 기본 지식으로 답변합니다."
        else:
            is_new = "신규 학습됨" in knowledge_context
            await status_msg.edit_text("💡 " + ("새로 수집한 정보로 " if is_new else "") + "답변 작성 중...")
            prompt += (
                f"{knowledge_context}{graph_context}\n\n"
                "위 [로컬 지식 검색 결과]를 최우선으로 참고해서 답변해줘.\n"
                "답변에 사용한 지식 출처는 반드시 [[페이지명]] 형식으로 명시해줘.\n"
                "출처가 여러 개면 모두 나열해줘. (예: [[RTK-AI]], [[Claude-Tips]])"
            )

        chat_messages = _truncate_messages(history_msgs)
        response = await ask_ollama(prompt, system_prompt=system_prompt, messages=chat_messages)

        cleaned = _strip_tags(response.strip())
        await status_msg.delete()
        await _send_long(update, cleaned)
        await save_chat(user_id, "assistant", cleaned)
        await check_and_summarize(user_id)

    except Exception as e:
        await status_msg.edit_text(f"❌ 지식 답변 중 오류: {e}")


# ── CI / 루프 상태 ─────────────────────────────────────────────────────────────

async def cmd_ci_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth(update):
        return

    loop = asyncio.get_event_loop()

    def check_ci():
        try:
            from github import Github
        except ImportError:
            return "❌ PyGithub 라이브러리가 설치되지 않았습니다."

        # Prioritize environment variable for security
        token = os.environ.get("GITHUB_TOKEN") or CFG.get("github", {}).get("token", "")
        repo_name = CFG.get("github", {}).get("repo", "")
        if not token:
            return "❌ GitHub Token이 설정되지 않았습니다 (GITHUB_TOKEN 환경변수 필요)."
        if not repo_name:
            return "❌ GitHub 저장소 정보가 없습니다."
        
        try:
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
        except Exception as e:
            return f"❌ GitHub API 오류: {e}"

    try:
        result = await loop.run_in_executor(None, check_ci)
    except Exception as e:
        result = f"❌ CI 상태 확인 실패: {e}"
    await update.message.reply_text(result, parse_mode="HTML")


async def cmd_loop_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth(update):
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
    if not await _auth(update):
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
    
    log.warning(f"SECURITY AUDIT: User {update.effective_user.id} is executing SH: {cmd}")
    
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

    log.warning(f"SECURITY AUDIT: User {update.effective_user.id} is executing PY: {code}")

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
    if not await _auth(update):
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
    from services.weather_service import sync_all

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
    if not await _auth(update):
        return
    from services.weather_service import sync_all
    await update.message.reply_text("🌦️ 안성 날씨 정보 동기화 시작...", parse_mode="HTML")
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, sync_all)
        await update.message.reply_text("✅ 날씨 정보 동기화 완료! 이제 캐시된 정보를 사용합니다.", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ 동기화 실패: {_escape_html(str(e))}", parse_mode="HTML")


async def cmd_reinforce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """00_Raw/ → 10_Wiki/ 구조화 파이프라인 실행 후 RAG 재인덱싱"""
    if not await _auth(update):
        return
    msg = await update.message.reply_text("⚙️ reinforce 실행 중...", parse_mode="HTML")
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "reinforce.py",
            cwd="/home/home/Jin_lifestyle",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode == 0:
            output = stdout.decode().strip() or "출력 없음"
            lines = [l for l in output.splitlines() if any(k in l for k in ["✨", "✅", "📄", "📦", "🚀", "❌", "📭"])]
            summary = "\n".join(lines) if lines else output[:500]
            await msg.edit_text(f"✅ reinforce 완료\n<pre>{_escape_html(summary)}</pre>", parse_mode="HTML")
        else:
            await msg.edit_text(f"❌ reinforce 실패\n<pre>{_escape_html(stderr.decode()[:500])}</pre>", parse_mode="HTML")
            return
    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ 타임아웃 (5분 초과)", parse_mode="HTML")
        return
    except Exception as e:
        await msg.edit_text(f"❌ reinforce 오류: {_escape_html(str(e))}", parse_mode="HTML")
        return

    if RAG_ENABLED:
        rag_msg = await update.message.reply_text("📚 RAG 재인덱싱 중...", parse_mode="HTML")
        try:
            rag_result = await rag_manager.index_knowledge()
            await rag_msg.edit_text(rag_result)
        except Exception as e:
            await rag_msg.edit_text(f"❌ RAG 인덱싱 오류: {_escape_html(str(e))}", parse_mode="HTML")


async def cmd_reindex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ChromaDB 초기화 후 10_Wiki + LLM WIKI 전체 재인덱싱"""
    if not _admin(update):
        return
    if not RAG_ENABLED:
        await update.message.reply_text("❌ RAG 기능이 비활성화되어 있습니다.")
        return
    msg = await update.message.reply_text("🗑️ ChromaDB 초기화 중...", parse_mode="HTML")
    try:
        result = await rag_manager.reset_and_reindex()
        await msg.edit_text(f"✅ 재인덱싱 완료\n{result}")
    except Exception as e:
        await msg.edit_text(f"❌ 재인덱싱 오류: {_escape_html(str(e))}", parse_mode="HTML")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """위키 현황 통계 (/stats)"""
    if not await _auth(update):
        return
    try:
        # stats = wiki_navigator.wiki_stats()
        stats = {"info": "wiki_navigator.py 실종으로 통계 비활성화"}
        cat = stats["category_counts"]
        cat_lines = "\n".join(
            f"  • {k}: {v}개" for k, v in cat.items()
        ) or "  (카테고리 없음)"

        recent = stats["recent_log"]
        recent_lines = "\n".join(f"  {l}" for l in recent) if recent else "  (이력 없음)"

        text = (
            f"📊 <b>위키 현황</b>\n\n"
            f"📄 총 페이지: <b>{stats['total_index']}개</b>\n"
            f"🔗 지식 연결: <b>{stats['total_edges']}개</b> 엣지\n"
            f"📦 00_Raw 미처리: <b>{stats['raw_pending']}개</b>\n"
            f"🕒 그래프 갱신: {stats['graph_updated']}\n\n"
            f"<b>카테고리별</b>\n{cat_lines}\n\n"
            f"<b>최근 강화 이력</b>\n{recent_lines}"
        )
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ 통계 오류: {_escape_html(str(e))}", parse_mode="HTML")


async def cmd_lint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """위키 건강 점검 (/lint) — 깨진 링크, 고아 페이지, index 불일치"""
    if not await _auth(update):
        return
    msg = await update.message.reply_text("🔍 위키 점검 중...", parse_mode="HTML")
    try:
        import sys
        _jin_root = "/home/home/Jin_lifestyle"
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c",
            (
                "import sys; sys.path.insert(0, '.'); "
                "from reinforce import WikiLinter; import json; "
                "linter = WikiLinter(); print(json.dumps(linter.run(), ensure_ascii=False))"
            ),
            cwd=_jin_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            await msg.edit_text(f"❌ 점검 실패\n<pre>{_escape_html(stderr.decode()[:300])}</pre>", parse_mode="HTML")
            return

        import json as _json
        result = _json.loads(stdout.decode())

        lines = ["🏥 <b>위키 건강 점검 결과</b>\n"]

        raw_pending = result.get("raw_pending", [])
        lines.append(f"📥 미처리 raw: <b>{len(raw_pending)}개</b>")

        broken_refs = result.get("broken_refs", [])
        if broken_refs:
            lines.append(f"\n❌ index에만 있고 파일 없음 ({len(broken_refs)}개):")
            for t in broken_refs[:5]:
                lines.append(f"  • {_escape_html(t)}")

        unregistered = result.get("unregistered", [])
        if unregistered:
            lines.append(f"\n⚠️ 파일만 있고 index 미등록 ({len(unregistered)}개):")
            for t in unregistered[:5]:
                lines.append(f"  • {_escape_html(t)}")

        orphans = result.get("orphans", [])
        if orphans:
            lines.append(f"\n🏝️ 고아 페이지 — 연결 없음 ({len(orphans)}개):")
            for t in orphans[:5]:
                lines.append(f"  • {_escape_html(t)}")

        broken_links = result.get("broken_links", {})
        if broken_links:
            lines.append(f"\n🔗 깨진 wikilink ({len(broken_links)}개 파일):")
            for fname, blinks in list(broken_links.items())[:3]:
                lines.append(f"  • {_escape_html(fname)}: {', '.join(_escape_html(b) for b in blinks[:3])}")

        total = result.get("total_wiki", 0)
        indexed = result.get("total_indexed", 0)
        nodes = result.get("total_graph_nodes", 0)
        lines.append(f"\n📊 파일 {total} | 인덱스 {indexed} | 그래프 노드 {nodes}")

        if not any([broken_refs, unregistered, orphans, broken_links]):
            lines.append("\n✅ 위키 상태 양호!")

        await msg.edit_text("\n".join(lines), parse_mode="HTML")
    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ 점검 타임아웃 (60초 초과)", parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ 점검 오류: {_escape_html(str(e))}", parse_mode="HTML")


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """웹 검색 + AI 요약 (/search <검색어>)"""
    if not await _auth(update):
        return
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("사용법: /search &lt;검색어&gt;", parse_mode="HTML")
        return

    status_msg = await update.message.reply_text("🔍 네이버 검색 중...")
    loop = asyncio.get_event_loop()
    try:
        raw_results = await loop.run_in_executor(None, lambda: _search_naver(query))
        await status_msg.edit_text("💡 검색 결과 요약 중...")
        prompt = (
            f"검색어: {query}\n\n"
            f"검색 결과:\n{raw_results}\n\n"
            f"위 검색 결과를 바탕으로 핵심 내용을 한국어로 간결하게 요약해줘.\n\n"
            f"{SEARCH_SYSTEM_PROMPT}"
        )
        summary = await ask_ollama(prompt)
        cleaned = _strip_tags(summary.strip())
        await status_msg.delete()
        await _send_long(update, cleaned)
    except Exception as e:
        await status_msg.edit_text(f"❌ 검색 오류: {_escape_html(str(e))}", parse_mode="HTML")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """대화 히스토리 초기화"""
    if not await _auth(update):
        return
    chat_id = update.effective_chat.id
    log.warning(f"SECURITY AUDIT: User {update.effective_user.id} cleared history for chat {chat_id}")
    _clear_history(chat_id)
    await update.message.reply_text("🗑️ 대화 기록이 초기화되었습니다.")


async def cmd_clip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """URL 클리핑 → 옵시디언 양식으로 00_Raw 저장 + reinforce 트리거 (/clip <URL>)"""
    if not await _auth(update):
        return

    url = context.args[0] if context.args else ""
    if not url or not url.startswith(("http://", "https://")):
        await update.message.reply_text(
            "사용법: /clip &lt;URL&gt;\n\n"
            "예시:\n"
            "  /clip https://example.com/article\n"
            "  /clip https://youtube.com/watch?v=abc\n"
            "  /clip https://arxiv.org/abs/2301.00001",
            parse_mode="HTML",
        )
        return

    # 소스 타입 아이콘 매핑
    _type_icons = {
        "article": "📰", "youtube": "🎬", "research": "🔬",
        "book": "📚", "podcast": "🎙️",
    }

    status_msg = await update.message.reply_text("🔗 페이지 수집 중...")

    try:
        from core.agent import get_agent
        agent = get_agent()

        # YouTube인 경우 상태 메시지 업데이트
        if any(t in url for t in ("youtube.com", "youtu.be")):
            try:
                await status_msg.edit_text("🎬 YouTube 메타데이터 + 자막 추출 중...")
            except Exception:
                pass

        result = await agent.clip_url(url, notify_chat_id=update.effective_chat.id, notify_message_id=status_msg.message_id)

        if not result["success"]:
            await status_msg.edit_text(f"❌ 클리핑 실패: {_escape_html(result.get('error', '알 수 없는 오류'))}", parse_mode="HTML")
            return

        icon = _type_icons.get(result["source_type"], "📄")
        title = _escape_html(result["title"][:60])
        text_len = result["text_length"]

        lines = [f"✅ {icon} <b>{result['source_type'].capitalize()}</b> 클리핑 완료\n"]
        lines.append(f"📄 <b>{title}</b>")
        lines.append(f"📊 본문: {text_len:,}자")
        lines.append(f"💾 저장: <code>{_escape_html(result['filename'])}</code>")

        if result.get("duplicate"):
            lines.append(f"\n⚠️ 이미 클리핑된 URL입니다: <code>{_escape_html(result['duplicate'])}</code>")

        lines.append("\n⚙️ reinforce 파이프라인 자동 실행 중...")

        await status_msg.edit_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        log.error(f"cmd_clip 오류: {e}")
        await status_msg.edit_text(f"❌ 클리핑 오류: {_escape_html(str(e))}", parse_mode="HTML")

async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """AI 태스크 큐 관리 (/task <설명> | status | cancel <id>)"""
    if not _admin(update):
        return
    from core.task_queue import create_task, cancel_task, list_tasks, get_task, STATES

    args = context.args or []

    # ── /task status [id] ─────────────────────────────────────────────────
    if args and args[0] == "status":
        # 특정 태스크 상세보기
        if len(args) >= 2:
            t = get_task(args[1])
            if not t:
                await update.message.reply_text(f"❌ 태스크 <code>{args[1]}</code> 없음", parse_mode="HTML")
                return
            icons = {"pending": "⏳", "in_progress": "🔄", "completed": "✅",
                     "cancelled": "🚫", "failed": "❌"}
            result_text = _escape_html(str(t.get("result") or "-")[:120])
            await update.message.reply_text(
                f"{icons.get(t['status'], '?')} <b>태스크 상세</b>\n\n"
                f"🆔 <code>{t['id']}</code>\n"
                f"상태: <b>{t['status']}</b>\n"
                f"브랜치: <code>{t['branch']}</code>\n"
                f"재시도: {t.get('retries', 0)}/{t.get('max_retries', 5)}\n"
                f"생성: {t['created'][:16]}\n"
                f"📝 {_escape_html(t['description'][:100])}\n\n"
                f"결과: {result_text}",
                parse_mode="HTML"
            )
            return

        # 전체 현황 요약
        lines = ["📋 <b>AI 태스크 현황</b>\n"]
        icons = {"pending": "⏳", "in_progress": "🔄", "completed": "✅",
                 "cancelled": "🚫", "failed": "❌"}
        total = 0
        for state in STATES:
            tasks = list_tasks(state)
            if not tasks:
                continue
            total += len(tasks)
            lines.append(f"{icons[state]} <b>{state.upper()}</b> ({len(tasks)}개)")
            for t in tasks[:3]:
                retry_info = f" [재시도 {t['retries']}]" if t.get("retries") else ""
                desc_short = _escape_html(t["description"][:45])
                lines.append(f"  <code>{t['id']}</code> {desc_short}{retry_info}")
            if len(tasks) > 3:
                lines.append(f"  ...외 {len(tasks) - 3}개")
        if total == 0:
            lines.append("현재 태스크 없음.")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    # ── /task cancel <id> ─────────────────────────────────────────────────
    if args and args[0] == "cancel":
        if len(args) < 2:
            await update.message.reply_text("사용법: /task cancel <id>")
            return
        task = cancel_task(args[1])
        if not task:
            await update.message.reply_text(f"❌ 태스크 <code>{args[1]}</code> 를 찾을 수 없습니다.", parse_mode="HTML")
            return

        # 브랜치 삭제 시도 (로컬 + 원격)
        branch = task.get("branch", "")
        branch_msg = ""
        if branch:
            from core.bot_config import CFG
            from pathlib import Path
            work_dir = Path(CFG["agent"].get("work_dir", ".")).resolve()
            proc = await asyncio.create_subprocess_exec(
                "git", "branch", "-D", branch,
                cwd=str(work_dir),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
            proc2 = await asyncio.create_subprocess_exec(
                "git", "push", "origin", "--delete", branch,
                cwd=str(work_dir),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc2.wait()
            branch_msg = f"\n🗑️ 브랜치 삭제: <code>{branch}</code>"

        await update.message.reply_text(
            f"🚫 태스크 취소됨\n"
            f"<code>{task['id']}</code>: {_escape_html(task['description'][:60])}"
            f"{branch_msg}",
            parse_mode="HTML"
        )
        return

    # ── /task <description> ───────────────────────────────────────────────
    description = " ".join(args).strip()
    if not description:
        await update.message.reply_text(
            "사용법:\n"
            "  <code>/task &lt;작업 설명&gt;</code> — 새 태스크 등록\n"
            "  <code>/task status</code> — 태스크 목록\n"
            "  <code>/task cancel &lt;id&gt;</code> — 태스크 취소",
            parse_mode="HTML"
        )
        return

    task = create_task(description, source="chat")
    await update.message.reply_text(
        f"✅ 태스크 등록됨\n\n"
        f"🆔 <code>{task['id']}</code>\n"
        f"🌿 브랜치: <code>{task['branch']}</code>\n"
        f"📝 {_escape_html(description[:100])}",
        parse_mode="HTML"
    )


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """모델 백엔드 전환 (관리자 전용): /switch lm | /switch ollama"""
    if not _admin(update):
        await update.message.reply_text("🔒 관리자 전용 명령입니다.")
        return

    env_path = Path(__file__).parent.parent.parent / ".env"

    def _set_env(key: str, val: str):
        text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        lines = text.splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={val}"
                found = True
                break
        if not found:
            lines.append(f"{key}={val}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    arg = (context.args[0] if context.args else "").lower()

    async def _restart():
        import sys, os
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    if arg in ("lm", "lm-studio"):
        _set_env("MODEL_BACKEND", "llama-cpp")
        _set_env("EMBEDDING_BACKEND", "lm-studio")
        await update.message.reply_text("✅ LM Studio 모드로 전환됨 (포트 1234)\n🔄 봇 재시작 중...")
        asyncio.create_task(_restart())
    elif arg == "ollama":
        _set_env("MODEL_BACKEND", "ollama")
        _set_env("EMBEDDING_BACKEND", "ollama")
        await update.message.reply_text("✅ Ollama 모드로 전환됨 (포트 11434)\n🔄 봇 재시작 중...")
        asyncio.create_task(_restart())
    elif arg == "unsloth":
        _set_env("MODEL_BACKEND", "unsloth")
        await update.message.reply_text("✅ Unsloth Studio 모드로 전환됨 (포트 8888)\n🔄 봇 재시작 중...")
        asyncio.create_task(_restart())
    else:
        import core.bot_config as _cfg
        current = _cfg.MODEL_BACKEND
        embed = _cfg.EMBEDDING_BACKEND
        await update.message.reply_text(
            f"현재: <code>{current}</code> / 임베딩: <code>{embed}</code>\n\n"
            f"사용법:\n/switch lm — LM Studio\n/switch ollama — Ollama\n/switch unsloth — Unsloth Studio",
            parse_mode="HTML"
        )
