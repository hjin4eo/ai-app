#!/usr/bin/env python3
"""
bot_utils.py — 텍스트 유틸, 인증, 채팅 저장, 모델 호출
의존성: bot_config
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Lock

from telegram import Update

from bot_config import (
    ALLOWED_CHAT_IDS,
    DATA_DIR,
    LLAMA_CPP_URL,
    MODEL_BACKEND,
    OLLAMA_MODEL,
    OLLAMA_URL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_FORMAT_RULES,
    _auth_fail_notified,
    OLLAMA_NUM_CTX,
    CHAT_HISTORY_LIMIT,
)
from telegram.helpers import escape_markdown
import time
import traceback

# ── 대화 기록 설정 ────────────────────────────────────────────────────────────
HISTORY_DIR = Path(__file__).parent.parent / ".history"
MAX_HISTORY = 20       # 최근 20턴 (40 메시지)
MAX_CHARS   = 12000    # messages 총 글자 수 제한 (컨텍스트 초과 방지)
CHAT_SYSTEM_PROMPT = (
    "너는 도움이 되는 AI 어시스턴트야. 한국어로 대답해.\n\n"
    + TELEGRAM_FORMAT_RULES
)
SEARCH_SYSTEM_PROMPT = (
    "너는 웹 검색 결과를 요약하는 AI야. 한국어로 답해.\n\n"
    + TELEGRAM_FORMAT_RULES
)
_history_lock = Lock()
_summary_lock = asyncio.Lock()  # 비동기 요약용 락

log = logging.getLogger(__name__)

def is_local_query(text: str) -> bool:
    """로컬 파일/경로 관련 요청인지 판별 (전문가 제안 로직)"""
    if len(text) < 5:
        return False
    keywords = ["폴더", "파일", "경로", "디렉토리", "소스", "코드", "수정", "탐색"]
    has_keyword = any(k in text for k in keywords)
    has_path = "/" in text or "\\" in text
    return has_keyword or has_path

def escape_safe(text: str) -> str:
    """코드블록을 보호하면서 Telegram Markdown V2용 이스케이프 처리"""
    parts = text.split("```")
    for i in range(len(parts)):
        if i % 2 == 0:  # 일반 텍스트 부분만 이스케이프
            # MarkdownV2에서 요구하는 특수문자들 이스케이프
            parts[i] = escape_markdown(parts[i], version=2)
    return "```".join(parts)


# ── 텍스트 유틸 ───────────────────────────────────────────────────────────────

def _strip_tags(text: str) -> str:
    """HTML 태그와 Markdown 기호를 강제 제거."""
    text = re.sub(r'<[^>]+>', '', text)           # 모든 HTML 태그 제거
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group().strip('`'), text)  # 코드블록 ``` 제거
    text = re.sub(r'(?<!\w)\*{1,3}([^*]+)\*{1,3}(?!\w)', r'\1', text)      # *bold*, **bold**, ***bold***
    text = re.sub(r'(?<!\w)_{1,3}([^_]+)_{1,3}(?!\w)', r'\1', text)        # _italic_
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)             # # 헤더
    return text.strip()


def _escape_html(text: str) -> str:
    """HTML parse_mode에서 안전하게 사용할 수 있도록 특수문자 이스케이프"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_markup(text: str) -> str:
    """AI 응답에서 HTML 태그와 Markdown 서식을 제거하여 순수 텍스트로 변환"""
    text = re.sub(r'<[^>]+>', '', text)           # HTML 태그 제거
    text = re.sub(r'```[\s\S]*?```', '', text)     # 코드 블록
    text = re.sub(r'`([^`]+)`', r'\1', text)       # 인라인 코드
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # **bold**
    text = re.sub(r'__(.+?)__', r'\1', text)       # __bold__
    text = re.sub(r'\*(.+?)\*', r'\1', text)       # *italic*
    text = re.sub(r'_(.+?)_', r'\1', text)         # _italic_
    text = re.sub(r'#{1,6}\s*', '', text)          # ### 헤더
    # 테이블 처리: 구분선 제거, 셀 내용만 추출
    text = re.sub(r'^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\|(.+)\|$', lambda m: '  '.join(cell.strip() for cell in m.group(1).split('|') if cell.strip()), text, flags=re.MULTILINE)
    # 기타 마크다운
    text = re.sub(r'^\s*[-*+]\s+', '- ', text, flags=re.MULTILINE)  # 리스트 통일
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)    # 번호 리스트 번호 제거
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)            # [링크텍스트](url) → 링크텍스트
    text = re.sub(r'~{2}(.+?)~{2}', r'\1', text)                    # ~~취소선~~
    # 빈 줄 정리
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _sanitize_html(text: str) -> str:
    """텔레그램에서 지원하지 않는 HTML 태그를 제거"""
    allowed = {'b', 'i', 'code', 'pre', 'a', 's', 'u'}

    def _replace_tag(m):
        tag_name = re.match(r'</?(\w+)', m.group(0))
        if tag_name and tag_name.group(1).lower() in allowed:
            return m.group(0)
        return ''

    text = re.sub(r'</?[a-zA-Z][^>]*>', _replace_tag, text)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    return text


async def _send_long(update: Update, text: str, parse_mode: str = None) -> None:
    """긴 메시지를 4000자 단위로 쪼개서 전송."""
    for i in range(0, len(text), 4000):
        try:
            await update.message.reply_text(text[i : i + 4000], parse_mode=parse_mode)
        except Exception as e:
            log.warning(f"전송 실패, 일반 텍스트로 보냅니다: {e}")
            await update.message.reply_text(text[i : i + 4000], parse_mode=None)


# ── 인증 ─────────────────────────────────────────────────────────────────────

def _auth(update: Update) -> bool:
    """관리자 + 허용된 일반 사용자"""
    chat_id = update.effective_chat.id
    if chat_id not in ALLOWED_CHAT_IDS:
        user = update.effective_user
        log.warning("인증 실패: chat_id=%s user=%s (@%s)", chat_id, user.full_name if user else "?", user.username if user else "?")
        today = datetime.now().strftime("%Y-%m-%d")
        if _auth_fail_notified.get(chat_id) != today:
            _auth_fail_notified[chat_id] = today
            name = user.full_name if user else "알 수 없음"
            username = f"@{user.username}" if user and user.username else "없음"
            import urllib.request as _ur
            msg = f"🔒 인증 실패 알림\n\nchat_id: {chat_id}\n이름: {name}\n유저네임: {username}\n\n허용하려면:\n/allow {chat_id}"
            payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": msg}).encode()
            req = _ur.Request(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            try:
                _ur.urlopen(req, timeout=5)
            except Exception:
                pass
        return False
    return True


def _admin(update: Update) -> bool:
    """관리자만 (위험 명령용)"""
    return update.effective_chat.id == TELEGRAM_CHAT_ID


# ── 대화 기록 관리 (신규) ──────────────────────────────────────────────────────

def _history_path(chat_id: int) -> Path:
    """채팅방별 전용 디렉토리에 히스토리 파일 경로 반환 (데이터 통합)"""
    chat_dir = get_user_dir(chat_id)
    return chat_dir / "chat_history.jsonl"


def _load_history(chat_id: int) -> list[dict]:
    """JSONL에서 최근 MAX_HISTORY*2개의 role/content dict 리스트 반환."""
    path = _history_path(chat_id)
    if not path.exists():
        return []
    messages = []
    try:
        content = path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
                    messages.append({"role": msg["role"], "content": msg["content"]})
            except json.JSONDecodeError:
                continue
    except Exception as e:
        log.error(f"히스토리 로드 오류 (chat {chat_id}): {e}")
    return messages[-(MAX_HISTORY * 2):]


def _save_history(chat_id: int, role: str, content: str) -> None:
    """메시지 한 건 저장 및 슬라이딩 윈도우(20턴) 적용."""
    if role not in ("user", "assistant"):
        return
    path = _history_path(chat_id)
    with _history_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"role": role, "content": content, "ts": datetime.now().isoformat()}, ensure_ascii=False) + "\n")
    
    # 슬라이딩 윈도우 보장 (최근 20턴 = 40개 메시지)
    _trim_history(chat_id, max_lines=40)


def _trim_history(chat_id: int, max_lines: int = 40) -> None:
    path = _history_path(chat_id)
    if not path.exists():
        return
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(lines) > max_lines:
        # 오래된 기록은 버리고 최신 max_lines만 유지
        lines = lines[-max_lines:]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clear_history(chat_id: int) -> None:
    """해당 chat의 대화 기록 파일 삭제."""
    path = _history_path(chat_id)
    if path.exists():
        path.unlink()


def _truncate_messages(messages: list[dict]) -> list[dict]:
    """messages 총 글자 수가 MAX_CHARS를 넘지 않도록 최근 메시지부터 유지."""
    total = 0
    result = []
    for msg in reversed(messages):
        total += len(msg.get("content") or "")
        if total > MAX_CHARS:
            break
        result.append(msg)
    return list(reversed(result))


# ── 채팅 저장 (레거시 — cmd_ask 등 기존 호환용) ──────────────────────────────

def get_user_dir(user_id: int) -> Path:
    user_dir = DATA_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def save_chat(user_id: int, role: str, content: str):
    user_dir = get_user_dir(user_id)
    history_file = user_dir / "chat_history.jsonl"
    with open(history_file, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": role, "content": content, "ts": datetime.now().isoformat()}, ensure_ascii=False) + "\n")


def load_context(chat_id: int, limit: int = CHAT_HISTORY_LIMIT) -> str:
    """채팅방별 요약과 최근 기록을 불러와 컨텍스트 구성."""
    user_dir = get_user_dir(chat_id) # chat_id 기반으로 통일
    history_file = _history_path(chat_id)
    summary_file = user_dir / "summary.txt"

    context_parts = []

    if summary_file.exists():
        summary = summary_file.read_text(encoding="utf-8")
        context_parts.append(f"### [이전 대화 요약]\n{summary}\n")

    messages = _load_history(chat_id)
    if messages:
        history_text = ""
        for msg in messages[-limit:]:
            role_name = "사용자" if msg["role"] == "user" else "AI"
            history_text += f"{role_name}: {msg['content']}\n"
        if history_text:
            context_parts.append(f"### [최근 대화 내역]\n{history_text}")

    return "\n".join(context_parts)


# ── 모델 호출 ─────────────────────────────────────────────────────────────────

def _ask_ollama(prompt: str, system_prompt: str = "", images: list | None = None,
                max_tokens: int = 8192, timeout: int = 300,
                num_ctx: int | None = None, keep_alive: str = "5m",
                messages: list[dict] | None = None) -> str:
    target_ctx = num_ctx if num_ctx else OLLAMA_NUM_CTX

    # messages 배열 구성: system → 기록 → 현재 user 메시지
    final_messages: list[dict] = []
    if system_prompt:
        final_messages.append({"role": "system", "content": system_prompt})
    if messages:
        final_messages.extend(messages)
    user_msg: dict = {"role": "user", "content": prompt}
    if images:
        user_msg["images"] = images
    final_messages.append(user_msg)

    payload_dict = {
        "model": OLLAMA_MODEL,
        "messages": final_messages,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {
            "num_predict": max_tokens,
            "num_ctx": target_ctx,
        },
    }

    payload = json.dumps(payload_dict).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["message"]["content"]


def _ask_llama_cpp(prompt: str, system_prompt: str = "", images: list | None = None, max_tokens: int = 8192, timeout: int = 300, **kwargs) -> str:
    # llama-cpp는 API 구조상 options 처리가 다를 수 있으므로 호환성을 위해 **kwargs 수용
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    if images:
        content = [{"type": "text", "text": prompt}]
        for img_b64 in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            })
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})

    payload = json.dumps({
        "messages": messages,
        "max_tokens": max_tokens,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{LLAMA_CPP_URL}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"]
    except urllib.request.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log.error("llama-cpp %s: %s", e.code, body)
        raise


def ask_ollama(prompt: str, system_prompt: str = "", images: list | None = None,
               max_tokens: int = 8192, timeout: int = 300,
               num_ctx: int | None = None, keep_alive: str = "5m",
               messages: list[dict] | None = None) -> str:
    if MODEL_BACKEND == "llama-cpp":
        return _ask_llama_cpp(prompt, system_prompt, images, max_tokens, timeout,
                              num_ctx=num_ctx, keep_alive=keep_alive)
    
    # Planner용 시스템 프롬프트에 로컬 검색 강조 추가 (전문가 팁)
    if "Planner" in system_prompt or "JSON" in system_prompt:
        system_prompt += (
            "\n[중요 지침]\n"
            "- 사용자가 '폴더', '파일', '경로', '디렉토리'를 언급하거나 로컬 환경 탐색 중이라면, "
            "절대로 웹 검색이 아닌 로컬 도구(read_file, list_dir 등)를 사용하도록 유도하십시오.\n"
            "- 확실하지 않은 경우 일반 지식으로 대답하지 말고 도구를 사용해 사실을 확인하십시오."
        )

    return _ask_ollama(prompt, system_prompt, images, max_tokens, timeout,
                       num_ctx=num_ctx, keep_alive=keep_alive, messages=messages)


# ── 파일시스템 툴 콜링 ────────────────────────────────────────────────────────

HOME_DIR = Path("/home/home")

FS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "파일 내용을 읽어서 반환. 코드, 설정, 로그 파일 확인에 사용.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "파일 절대 경로"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "디렉토리 내 파일/폴더 목록 반환.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "디렉토리 절대 경로. 기본: /home/home"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "파일에 내용을 씀. 코드 수정, 메모 저장에 사용.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "파일 절대 경로"},
                    "content": {"type": "string", "description": "저장할 내용"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "/home/home 아래에서 파일명 패턴으로 파일 검색.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "검색 패턴 (예: *.py, bot_*.py, *.md)"}
                },
                "required": ["pattern"]
            }
        }
    }
]


def _exec_tool(name: str, args: dict, allow_write: bool = False) -> str:
    """툴 함수 실행. /home/home 하위 경로만 허용."""
    try:
        if name == "read_file":
            path = Path(args["path"])
            if not path.resolve().is_relative_to(Path("/home/home")):
                return "오류: /home/home 하위 경로만 접근 가능"
            if not path.exists():
                return f"오류: 파일 없음 — {path}"
            return path.read_text(encoding="utf-8", errors="replace")[:8000]

        elif name == "list_dir":
            path = Path(args.get("path", "/home/home"))
            if not path.resolve().is_relative_to(Path("/home/home")):
                return "오류: /home/home 하위 경로만 접근 가능"
            if not path.exists():
                return f"오류: 디렉토리 없음 — {path}"
            lines = []
            for item in sorted(path.iterdir()):
                prefix = "📁" if item.is_dir() else "📄"
                lines.append(f"{prefix} {item.name}")
            return "\n".join(lines) or "(비어있음)"

        elif name == "write_file":
            if not allow_write:
                return "오류: 쓰기 권한 없음 (관리자만 가능)"
            path = Path(args["path"])
            if not path.resolve().is_relative_to(Path("/home/home")):
                return "오류: /home/home 하위 경로만 접근 가능"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"], encoding="utf-8")
            return f"✅ 저장 완료: {path}"

        elif name == "search_files":
            pattern = args.get("pattern", "*")
            results = list(HOME_DIR.rglob(pattern))
            results = [p for p in results if ".git" not in p.parts][:20]
            if not results:
                return "검색 결과 없음"
            return "\n".join(str(p) for p in results)

        return f"알 수 없는 툴: {name}"
    except Exception as e:
        log.error(f"툴 오류:\n{traceback.format_exc()}")
        return f"툴 오류: {e}"


def ask_ollama_with_tools(messages: list[dict], system_prompt: str = "",
                          allow_write: bool = False) -> str:
    """Ollama 툴 콜링 루프. 파일 탐색/읽기/쓰기 가능."""
    final_messages: list[dict] = []
    if system_prompt:
        final_messages.append({"role": "system", "content": system_prompt})
    final_messages.extend(messages)

    for _ in range(8):  # 최대 8번 툴 호출
        payload = {
            "model": OLLAMA_MODEL,
            "messages": final_messages,
            "tools": FS_TOOLS,
            "stream": False,
            "options": {"num_ctx": OLLAMA_NUM_CTX},
        }
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read())

        msg = result["message"]
        final_messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return msg.get("content", "")

        # 툴 실행 후 결과 추가
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_result = _exec_tool(fn.get("name", ""), fn.get("arguments", {}), allow_write)
            final_messages.append({"role": "tool", "content": tool_result})

    return final_messages[-1].get("content", "")


async def safe_summarize(chat_id: int):
    """락을 사용하여 안전하게 비동기 요약을 수행합니다."""
    async with _summary_lock:
        await check_and_summarize(chat_id)


async def check_and_summarize(chat_id: int):
    """역사 기록이 많아지면 요약본을 생성하고 기록을 정리합니다."""
    user_dir = get_user_dir(chat_id)
    history_file = _history_path(chat_id)
    if not history_file.exists():
        return

    with open(history_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if len(lines) > 30:
        log.info(f"Chat {chat_id}: 히스토리 30개 초과. 요약 시작...")
        to_summarize = lines[:20]
        remaining = lines[20:]

        old_text = ""
        for line in to_summarize:
            try:
                data = json.loads(line)
                old_text += f"{data['role']}: {data['content']}\n"
            except: continue

        summary_file = user_dir / "summary.txt"
        existing_summary = summary_file.read_text(encoding="utf-8") if summary_file.exists() else "없음"

        prompt = (
            f"다음은 기존 대화 요약본과 새로 추가된 대화 내용이야. "
            f"기존 정보를 바탕으로 새 내용의 핵심을 포함해 최신 상태로 다시 요약해줘.\n\n"
            f"[기존 요약]\n{existing_summary}\n\n"
            f"[추가된 대화]\n{old_text}\n\n"
            f"한글로 핵심만 간결하게 요약해."
        )

        event_loop = asyncio.get_event_loop()
        try:
            new_summary = await event_loop.run_in_executor(None, lambda: ask_ollama(prompt))
            summary_file.write_text(new_summary, encoding="utf-8")
            with open(history_file, "w", encoding="utf-8") as f:
                f.writelines(remaining)
            log.info(f"Chat {chat_id}: 요약 완료.")
        except Exception as e:
            log.error(f"요약 중 오류 발생: {e}")
