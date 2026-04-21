#!/usr/bin/env python3
"""
telegram_bot.py — Jin_lifestyle 통합 AI 봇 (ai-worker 구조 적용)
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import signal
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

def now_kst() -> datetime.datetime:
    """현재 KST 시각 반환."""
    return datetime.datetime.now(tz=KST)

def datetime_context() -> str:
    """LLM system_prompt에 주입할 현재 날짜/시간 문자열."""
    now = now_kst()
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    wd = weekdays[now.weekday()]
    return (
        f"[현재 날짜/시간] {now.strftime('%Y년 %m월 %d일')} ({wd}요일) "
        f"{now.strftime('%H:%M')} KST"
    )

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ── Import Setup ──────────────────────────────────────────────────────────────
_agent_dir = Path(__file__).parent
if str(_agent_dir) not in sys.path:
    sys.path.append(str(_agent_dir))

from core.bot_config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    ALLOWED_CHAT_IDS,
    KNOWLEDGE_DIR,
    RAG_ENABLED,
)
from core.bot_utils import (
    _auth,
    _admin,
    _strip_tags,
    _send_long,
    _save_history,
    _truncate_messages,
    ask_ollama,
    ask_ollama_with_tools,
    CHAT_SYSTEM_PROMPT,
)
from core.recovery import run_startup_recovery
from core.shared import SUPPORTED_CITIES
from handlers.bot_commands import (
    cmd_roadmap,
    cmd_ask,
    cmd_clip,
    cmd_index,
    cmd_reinforce,
    cmd_reindex,
    cmd_stats,
    cmd_lint,
    cmd_status,
    cmd_service,
    cmd_sh,
    cmd_allow,
    cmd_deny,
    cmd_users,
    cmd_clear,
    cmd_search,
    cmd_switch,
    cmd_ps,
    cmd_weather_sync,
    cmd_task,
    weather_sync_loop,
)
from services import rag_manager
from services.weather_service import get_cached_weather, sync_city

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── /help : 명령어 안내 ───────────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth(update):
        return
    text = (
        "🤖 <b>ai-worker 개발해봇</b>\n\n"

        "💬 <b>AI 대화</b>\n"
        "  일반 메시지 → AI 자동 답변\n"
        "  /search &lt;검색어&gt; — 웹 검색 + AI 요약\n"
        "  /ask &lt;질문&gt; — RAG 지식 검색\n"
        "  /clip &lt;URL&gt; — URL 클리핑 → 지식 저장\n"
        "  /clear — 대화 기록 초기화\n\n"

        "🛠 <b>AI 태스크 (개발 자동화)</b>\n"
        "  /task &lt;작업 설명&gt; — AI에게 개발 작업 요청\n"
        "  /task status — 전체 태스크 현황\n"
        "  /task status &lt;id&gt; — 특정 태스크 상세\n"
        "  /task cancel &lt;id&gt; — 태스크 취소\n\n"

        "🔀 <b>Git / 브랜치</b>\n"
        "  /ci_status — 최근 CI 실행 결과\n"
        "  /loop_status — AI 루프 실행 상태\n\n"

        "🖥 <b>시스템</b>\n"
        "  /status — 서버 상태 (CPU/RAM/GPU)\n"
        "  /ps — 프로세스 목록\n"
        "  /sh &lt;명령&gt; — 셸 명령 실행 (관리자)\n"
        "  /service &lt;이름&gt; &lt;start|stop|restart&gt; — 서비스 제어 (관리자)\n\n"

        "📚 <b>지식 관리</b>\n"
        "  /reinforce — 00_Raw → 10_Wiki 구조화\n"
        "  /reindex — ChromaDB 전체 재인덱싱 (관리자)\n"
        "  /index — RAG 인덱스 갱신 (관리자)\n"
        "  /stats — 위키 현황 통계\n"
        "  /lint — 위키 건강 점검\n\n"

        "⚙️ <b>설정 (관리자)</b>\n"
        "  /switch [lm|ollama] — 모델 백엔드 전환\n"
        "  /allow &lt;chat_id&gt; — 사용자 허용\n"
        "  /deny &lt;chat_id&gt; — 사용자 차단\n"
        "  /users — 허용 목록\n"
        "  /weather_sync — 날씨 동기화\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ── 상태 메시지 유틸 ──────────────────────────────────────────────────────────
async def _update_status(message, new_text: str) -> None:
    """메시지 텍스트가 달라질 때만 수정."""
    if not message:
        return
    try:
        if message.text == new_text:
            return
        await message.edit_text(new_text)
    except Exception as e:
        if "Message is not modified" not in str(e):
            logging.getLogger(__name__).warning(f"상태 메시지 업데이트 실패: {e}")

# ── 사진/이미지 핸들러 (Vision 분석 및 지식화) ───────────────────────────────
async def msg_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth(update):
        return
    status_msg = await update.message.reply_text("🖼️ 이미지 분석 및 지식화 시작...")
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        img_bytes = await file.download_as_bytearray()

        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        img_name = f"upload_{update.effective_user.id}_{int(asyncio.get_running_loop().time()) % 100000}.jpg"
        img_path = KNOWLEDGE_DIR / img_name

        # 비동기 파일 쓰기
        await asyncio.to_thread(img_path.write_bytes, img_bytes)

        result = await rag_manager.index_file(img_path)
        await status_msg.edit_text(f"✅ 사진 저장 및 인덱싱 완료.\n파일명: {img_name}\n{result}")
    except Exception as e:
        log.error(f"이미지 처리 오류: {e}")
        await _update_status(status_msg, f"❌ 이미지 처리 중 오류 발생: {e}")

# ── 문서 핸들러 (PDF, TXT 등 자동 학습) ──────────────────────────────────────
async def msg_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth(update):
        return
    doc = update.message.document
    if not doc or not doc.file_name:
        return

    filename = doc.file_name
    ext = Path(filename).suffix.lower()
    if ext not in (".pdf", ".md", ".txt", ".py", ".json", ".yaml", ".jpg", ".png", ".webp"):
        await update.message.reply_text(f"❌ 지원하지 않는 형식입니다: {ext}")
        return

    status_msg = await update.message.reply_text(f"📥 문서 다운로드 중: {filename}")
    try:
        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        file_path = KNOWLEDGE_DIR / filename

        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(custom_path=file_path)

        result = await rag_manager.index_file(file_path)
        await status_msg.edit_text(f"✅ '{filename}' 등록 및 인덱싱 완료.\n{result}")
    except Exception as e:
        log.error(f"문서 처리 오류: {e}")
        await _update_status(status_msg, f"❌ 문서 처리 중 오류 발생: {e}")

# ── 메인 메시지 핸들러 (Hybrid RAG Routing) ──────────────────────────────────
async def msg_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text("🤔 지식 검색/분석 중...")

    _bot_log = logging.getLogger(__name__)
    async def _status(new_text: str):
        _bot_log.info(f"[{chat_id}] Status: {new_text}")
        await _update_status(status_msg, new_text)

    try:
        from core.bot_utils import load_context
        from core.bot_config import DATA_DIR
        context_data = await load_context(chat_id, limit=20)
        history_msgs = context_data["messages"]
        summary_text = context_data["summary"]

        # ── [Step -1] 지시사항 조회 요청 감지 ───────────────────────────
        _view_keywords = ["시스템 프롬프트 보여", "시스템프롬프트 보여", "지시사항 보여", "지시사항 뭐야",
                          "시스템 프롬포트 보여", "시스템프롬포트 보여", "지시사항 알려", "프롬프트 뭐야",
                          "지시사항 뭐가", "지시사항 확인"]
        if any(kw in text for kw in _view_keywords):
            _instr_file = DATA_DIR / str(chat_id) / "instructions.txt"
            await status_msg.delete()
            if _instr_file.exists():
                content = _instr_file.read_text(encoding="utf-8").strip()
                await update.message.reply_text(f"📋 현재 지시사항:\n\n{content}" if content else "📋 저장된 지시사항이 없습니다.")
            else:
                await update.message.reply_text("📋 저장된 지시사항이 없습니다.")
            return

        # ── [Step -1b] 지시사항 수정/요약 요청 감지 ──────────────────────
        _edit_keywords = ["지시사항 수정", "지시사항 정리", "지시사항 요약", "프롬프트 수정", "프롬프트 정리",
                          "지시사항 삭제", "지시사항 초기화"]
        if any(kw in text for kw in _edit_keywords):
            _instr_file = DATA_DIR / str(chat_id) / "instructions.txt"
            existing = _instr_file.read_text(encoding="utf-8").strip() if _instr_file.exists() else ""
            if not existing:
                await status_msg.delete()
                await update.message.reply_text("📋 저장된 지시사항이 없습니다.")
                return
            if "초기화" in text or "삭제" in text:
                _instr_file.write_text("", encoding="utf-8")
                await status_msg.delete()
                await update.message.reply_text("🗑️ 지시사항을 초기화했습니다.")
                return
            # 수정/요약은 AI에게 위임
            await _status("✏️ 지시사항 정리 중...")
            edit_prompt = (
                f"아래는 현재 저장된 지시사항이야:\n\n{existing}\n\n"
                f"사용자 요청: {text}\n\n"
                "위 요청에 따라 지시사항을 수정/정리해줘. "
                "결과는 지시사항 내용만 출력해. 설명 없이."
            )
            new_content = await ask_ollama(edit_prompt)
            new_content = _strip_tags(new_content.strip())
            _instr_file.parent.mkdir(parents=True, exist_ok=True)
            _instr_file.write_text(new_content, encoding="utf-8")
            await status_msg.delete()
            await update.message.reply_text(f"✅ 지시사항을 수정했습니다:\n\n{new_content}")
            return

        # ── [Step -1c] 시스템 프롬프트 저장 요청 감지 ────────────────────
        _save_keywords = ["시스템 프롬프트에 적어", "시스템프롬프트에 적어", "지시사항에 저장", "지시사항에 추가",
                          "시스템 프롬포트에 적어", "시스템프롬포트에 적어"]
        if any(kw in text for kw in _save_keywords):
            _instr_file = DATA_DIR / str(chat_id) / "instructions.txt"
            _instr_file.parent.mkdir(parents=True, exist_ok=True)
            # 현재 메시지에서 저장할 내용 추출 (키워드 앞부분)
            _content = text
            for kw in _save_keywords:
                if kw in _content:
                    _content = _content.replace(kw, "").strip(" ,.:;-\n")
                    break
            if not _content:
                await status_msg.delete()
                await update.message.reply_text("저장할 내용을 함께 적어주세요.\n예: '문제는 1개씩만 내줘, 시스템 프롬프트에 적어줘'")
                return
            existing = _instr_file.read_text(encoding="utf-8") if _instr_file.exists() else ""
            _instr_file.write_text(existing + f"\n- {_content}", encoding="utf-8")
            await status_msg.delete()
            await update.message.reply_text(f"✅ 지시사항에 저장했습니다:\n「{_content}」")
            return

        # ── [Step 0] 대화 참조 질문 감지 → 검색 스킵 ────────────────────
        _followup_keywords = [
            "그럼", "그거", "그게", "그건", "아까", "방금", "이전에", "너가", "네가",
            "뭐라고", "다시", "그 답", "그 내용", "말했어", "했잖아", "설명했잖",
            "what did", "what were", "앞에서", "위에서",
        ]
        # 명시적 검색 의도 키워드 (있으면 짧아도 검색 실행)
        _search_intent = ["검색", "찾아", "알려줘", "뭐야", "뭐에요", "search", "날씨", "뉴스", "최신"]
        _has_search_intent = any(kw in text for kw in _search_intent)

        _is_followup = (
            any(kw in text for kw in _followup_keywords)
            or (len(text) <= 25 and not _has_search_intent)
        )

        # ── [Step 1] 날씨 키워드 감지 → 캐시 조회/동기화 ──────────────────
        _weather_keywords = ["날씨", "기온", "기상", "weather", "비 오", "눈 오", "미세먼지", "공기", "먼지"]
        weather_context = ""
        skip_rag = _is_followup  # 대화 참조 질문이면 검색 스킵

        if any(kw in text.lower().replace(" ", "") for kw in _weather_keywords):
            target_city = "안성"
            for city in SUPPORTED_CITIES:
                if city in text:
                    target_city = city
                    break

            await _status(f"🌦️ {target_city} 기상 데이터 조회 중...")
            cached = get_cached_weather(f"{target_city} 날씨")
            if not cached:
                success = await asyncio.to_thread(sync_city, target_city)
                if success:
                    cached = get_cached_weather(f"{target_city} 날씨")

            if cached:
                weather_context = (
                    f"\n\n[실시간 {target_city} 기상 관측 정보]\n{cached}\n"
                    "위 데이터는 기상청 API에서 직접 가져온 현재 시황입니다. "
                    "별도의 웹 검색 없이 이 정보로 즉각 답변하십시오."
                )
                skip_rag = True

        # ── [Step 2] RAG + Naver 웹 검색 병렬 실행 ─────────────────────
        knowledge_context = ""
        if RAG_ENABLED and not skip_rag:
            from core.agent import get_agent
            agent = get_agent()

            await _status("📂 지식 탐색 + 🌐 최신 정보 검색 중...")

            # RAG + Naver 웹 항상 동시 실행
            rag_result, naver_result = await asyncio.gather(
                rag_manager.query_knowledge(text),
                agent._fetch_naver(text),
            )

            parts = []
            if rag_result:
                parts.append(rag_result)
            if naver_result:
                parts.append(f"[Naver 최신 검색]\n{naver_result}")

            # RAG + Naver 둘 다 없으면 → 전체 외부 소스 확장
            if not parts:
                await _status("🌐 외부 데이터 심층 검색 중...")
                knowledge_context = await agent.process_query(text)
            else:
                knowledge_context = "\n\n".join(parts)

        # ── [Step 3] AI 답변 생성 ───────────────────────────────────────────
        await _status("💡 답변 작성 중...")

        # ── 쇼핑 가격 결과: 포맷 프롬프트로 Ollama 정리 ──────────────────
        _is_shopping = (
            knowledge_context
            and ("쇼핑 가격 정보" in knowledge_context or "NaverShopping" in knowledge_context)
        )
        if _is_shopping:
            lines = [
                l.strip() for l in knowledge_context.splitlines()
                if l.strip().startswith("- ") and "원" in l
            ]
            if lines:
                product_list = "\n".join(lines)
                format_prompt = (
                    f"[내질문]\n{text}\n\n"
                    f"[검색결과]\n{product_list}\n\n"
                    f"위 검색결과 기준으로 답변해줘."
                )
                format_system = (
                    "너는 상품 목록을 정리하는 어시스턴트야. "
                    "주어진 [검색결과]만 사용해서 답변해. "
                    "목록에 없는 정보는 절대 추가하지 마."
                )
                response = await ask_ollama(format_prompt, system_prompt=format_system)
                cleaned = _strip_tags(response.strip())
                await status_msg.delete()
                await _send_long(update, f"🛒 {text} — 네이버 쇼핑 기준\n\n{cleaned}")
                await _save_history(chat_id, "user", text)
                await _save_history(chat_id, "assistant", cleaned)
                return

        effective_text = text

        # ── 사용자 지시사항 로드 ──────────────────────────────────────────
        _instr_file = DATA_DIR / str(chat_id) / "instructions.txt"
        _user_instructions = _instr_file.read_text(encoding="utf-8").strip() if _instr_file.exists() else ""

        # ── 일반 AI 답변 (Ollama) ──────────────────────────────────────────
        system_prompt = f"{datetime_context()}\n\n{CHAT_SYSTEM_PROMPT}"
        if _user_instructions:
            system_prompt += (
                f"\n\n[사용자 지시사항 - 반드시 준수]"
                f"\n아래 지시사항은 절대 답변으로 출력하지 말고, 행동으로만 따를 것.\n"
                f"{_user_instructions}"
            )
        if history_msgs:
            system_prompt += "\n\n[중요] 위 대화 기록을 반드시 참고하여 맥락에 맞게 답변하라. 대화 흐름이 이어지는 경우 이전 주제를 유지하라."
        if summary_text:
            system_prompt += f"\n\n[이전 대화 요약]\n{summary_text}"
            
        if weather_context:
            system_prompt += (
                f"\n\n[실시간 날씨 강제 지시]\n{weather_context}\n"
                "사용자가 날씨를 물어본 경우, 위 데이터를 기반으로 즉시 답변하십시오. "
                "절대로 '인터넷에 접속할 수 없다'거나 '직접 확인하라'는 등의 답변을 하지 마십시오. "
                "위 데이터는 시스템이 실시간으로 제공한 사실(Fact)입니다."
            )
        if knowledge_context:
            system_prompt += f"\n\n[참고 지식]\n{knowledge_context}"

        if _admin(update):
            system_prompt += "\n너는 현재 시스템 파일 접근 도구(read_file, list_dir)를 사용할 수 있어."

        chat_messages = _truncate_messages(history_msgs)

        if _admin(update):
            all_msgs = [*chat_messages, {"role": "user", "content": effective_text}]
            response = await ask_ollama_with_tools(all_msgs, system_prompt=system_prompt, allow_write=True)
        else:
            response = await ask_ollama(effective_text, system_prompt=system_prompt, messages=chat_messages)

        # ── [Step 4] 전송 및 기록 저장 ─────────────────────────────────────
        cleaned = _strip_tags(response.strip())
        await status_msg.delete()
        await _send_long(update, cleaned)
        await _save_history(chat_id, "user", text)
        await _save_history(chat_id, "assistant", cleaned)

        # [보완] 자동 요약 체크
        from core.bot_utils import check_and_summarize
        await asyncio.create_task(check_and_summarize(chat_id))

    except Exception as e:
        logging.getLogger(__name__).error(f"채팅 처리 오류: {e}")
        await _update_status(status_msg, f"❌ 오류 발생: {e}")

# ── 메인 실행 루프 ────────────────────────────────────────────────────────────
async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logging.getLogger(__name__).error("Telegram 봇 토큰이 없습니다.")
        sys.exit(1)

    # 1. 시스템 복구
    await asyncio.to_thread(run_startup_recovery)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # 명령어 핸들러 등록
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("roadmap", cmd_roadmap))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ps", cmd_ps))
    app.add_handler(CommandHandler("sh", cmd_sh))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("index", cmd_index))
    app.add_handler(CommandHandler("reinforce", cmd_reinforce))
    app.add_handler(CommandHandler("reindex", cmd_reindex))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("lint", cmd_lint))
    app.add_handler(CommandHandler("weather_sync", cmd_weather_sync))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("service", cmd_service))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("clip", cmd_clip))
    app.add_handler(CommandHandler("task", cmd_task))

    # 미디어 핸들러
    app.add_handler(MessageHandler(filters.PHOTO, msg_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, msg_document))

    # 텍스트 메시지 핸들러
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_chat))

    start_time = now_kst()
    log.info(
        "ai-worker Telegram 봇 시작 | admin=%s | %s",
        TELEGRAM_CHAT_ID,
        start_time.strftime("%Y-%m-%d %H:%M:%S KST"),
    )

    async with app:
        # 명령어 메뉴 등록
        commands = [
            BotCommand("help", "명령어 안내"),
            BotCommand("search", "웹 검색 + AI 요약"),
            BotCommand("ask", "지식 기반 질문 답변"),
            BotCommand("index", "전체 지식 수동 학습 (관리자)"),
            BotCommand("reinforce", "00_Raw → 10_Wiki 구조화 파이프라인"),
            BotCommand("reindex", "DB 초기화 후 전체 재인덱싱 (관리자)"),
            BotCommand("stats", "위키 현황 통계"),
            BotCommand("lint", "위키 건강 점검"),
            BotCommand("status", "서버 상태 확인"),
            BotCommand("ps", "프로세스 목록"),
            BotCommand("sh", "셸 명령 실행 (관리자)"),
            BotCommand("service", "서비스 제어 (관리자)"),
            BotCommand("weather_sync", "날씨 동기화"),
            BotCommand("allow", "사용자 허용 (관리자)"),
            BotCommand("deny", "사용자 차단 (관리자)"),
            BotCommand("users", "허용 목록 (관리자)"),
            BotCommand("clear", "대화 기록 초기화"),
            BotCommand("switch", "모델 백엔드 전환 lm/ollama (관리자)"),
            BotCommand("clip", "URL 클리핑 → 지식 저장"),
        ]
        await app.bot.set_my_commands(commands)

        # 날씨 백그라운드 동기화 태스크
        asyncio.create_task(weather_sync_loop())

        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        await stop_event.wait()
        await app.updater.stop()
        await app.stop()

    log.info("ai-worker Telegram 봇 종료")

if __name__ == "__main__":
    asyncio.run(main())
