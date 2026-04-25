#!/usr/bin/env python3
"""
shared.py — 플랫폼 무관 공용 유틸리티
Telegram/Discord 양쪽에서 공유하는 텍스트 처리, 대화 기록, 모델 호출 로직.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Lock

from core.bot_config import (
    DATA_DIR,
    LLAMA_CPP_URL,
    LLAMA_CPP_MODEL,
    MODEL_BACKEND,
    OLLAMA_MODEL,
    OLLAMA_URL,
    OLLAMA_NUM_CTX,
    CHAT_HISTORY_LIMIT,
    TELEGRAM_FORMAT_RULES,
)

log = logging.getLogger(__name__)

# ── 지원 도시 목록 (전국 85개 시 단위) ──────────────────────────────────────────
SUPPORTED_CITIES = [
    "안성", "보개면", "죽산면", "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "수원", "성남", "의정부", "안양", "부천", "광명", "평택", "동두천", "안산", "고양", 
    "과천", "구리", "남양주", "오산", "시흥", "군포", "의왕", "하남", "용인", "파주", 
    "이천", "김포", "화성", "양주", "포천", "여주", "춘천", "원주", "강릉", "동해", 
    "태백", "속초", "삼척", "청주", "충주", "제천", "천안", "공주", "보령", "아산", 
    "서산", "논산", "계룡", "당진", "전주", "군산", "익산", "정읍", "남원", "김제", 
    "목포", "여수", "순천", "나주", "광양", "포항", "경주", "김천", "안동", "구미", 
    "영주", "영천", "상주", "문경", "경산", "창원", "진주", "통영", "사천", "김해", 
    "밀양", "거제", "양산", "제주", "서귀포"
]

__all__ = [
    'is_local_query', '_strip_tags', '_escape_html', '_strip_markup', '_sanitize_html',
    'get_user_dir', 'save_chat', 'load_context', '_load_history', '_save_history',
    '_clear_history', '_trim_history', '_truncate_messages',
    '_ask_ollama', '_ask_llama_cpp', 'ask_ollama', 'ask_ollama_with_tools',
    'FS_TOOLS', '_exec_tool', 'safe_summarize', 'check_and_summarize',
    'HISTORY_DIR', 'MAX_HISTORY', 'MAX_CHARS', 'HOME_DIR',
    'CHAT_SYSTEM_PROMPT', 'SEARCH_SYSTEM_PROMPT', 'SUPPORTED_CITIES'
]

# ── 상수 ─────────────────────────────────────────────────────────────────────
HISTORY_DIR = Path(__file__).parent.parent.parent / ".history"
MAX_HISTORY = 20
MAX_CHARS   = 12000
HOME_DIR    = Path("/home/home")

CHAT_SYSTEM_PROMPT = (
    "너는 도움이 되는 AI 어시스턴트야. 한국어로 대답해.\n"
    "시스템이 제공하는 [실시간 기상 관측 정보]가 있다면, 이를 너의 지식보다 우선하여 사실로 받아들이고 답변해.\n"
    "특히 날씨 정보를 제공할 때는 데이터에 포함된 미세먼지(대기질) 상태를 반드시 함께 요약해서 알려줘.\n"
    "절대 '날씨 정보를 가져올 수 없다'거나 '인터넷 연결이 안 된다'는 답변은 하지 마.\n\n"
    + TELEGRAM_FORMAT_RULES
)
SEARCH_SYSTEM_PROMPT = (
    "너는 웹 검색 결과를 요약하는 AI야. 한국어로 답해.\n\n"
    + TELEGRAM_FORMAT_RULES
)

_history_lock = Lock()
_summary_lock = asyncio.Lock()

# ── 텍스트 유틸 ───────────────────────────────────────────────────────────────

def is_local_query(text: str) -> bool:
    if len(text) < 5:
        return False
    keywords = ["폴더", "파일", "경로", "디렉토리", "소스", "코드", "수정", "탐색"]
    return any(k in text for k in keywords) or "/" in text or "\\" in text


def _strip_tags(text: str) -> str:
    """HTML 태그와 Markdown 기호를 강제 제거."""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group().strip('`'), text)
    text = re.sub(r'(?<!\w)\*{1,3}([^*]+)\*{1,3}(?!\w)', r'\1', text)
    text = re.sub(r'(?<!\w)_{1,3}([^_]+)_{1,3}(?!\w)', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    return text.strip()


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_markup(text: str) -> str:
    """AI 응답에서 HTML 태그와 Markdown 서식을 제거하여 순수 텍스트로 변환."""
    # 1. HTML 태그 제거
    text = re.sub(r'<[^>]+>', '', text)
    # 2. 코드 블록 및 인라인 코드 제거
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # 3. 굵게, 기울임, 취소선 제거
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'~{2}(.+?)~{2}', r'\1', text)
    # 4. 헤더 기호 제거 (#, ## 등)
    text = re.sub(r'#{1,6}\s*', '', text)
    # 5. 리스트 마커 제거 (숫자형, 불렛형)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # 6. 링크 마크다운 처리 [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # 7. 인용구 기호 제거 (>)
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
    # 8. 수평선 제거 (---, *** 등)
    text = re.sub(r'^\s*[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # 9. 테이블 구조 제거 (구분선 및 파이프)
    text = re.sub(r'^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\|(.+)\|$', lambda m: '  '.join(cell.strip() for cell in m.group(1).split('|') if cell.strip()), text, flags=re.MULTILINE)
    text = re.sub(r'\|', ' ', text)
    # 10. 연속된 줄바꿈 정리
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _sanitize_html(text: str) -> str:
    allowed = {'b', 'i', 'code', 'pre', 'a', 's', 'u'}
    def _replace_tag(m):
        tag_name = re.match(r'</?(\w+)', m.group(0))
        if tag_name and tag_name.group(1).lower() in allowed:
            return m.group(0)
        return ''
    text = re.sub(r'</?[a-zA-Z][^>]*>', _replace_tag, text)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    return text


# ── 대화 기록 ─────────────────────────────────────────────────────────────────

def get_user_dir(user_id: int, prefix: str = "") -> Path:
    dirname = f"{prefix}{user_id}" if prefix else str(user_id)
    user_dir = DATA_DIR / dirname
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def _history_path(chat_id: int, prefix: str = "") -> Path:
    chat_dir = get_user_dir(chat_id, prefix)
    return chat_dir / "chat_history.jsonl"


def _load_history(chat_id: int, prefix: str = "") -> list[dict]:
    path = _history_path(chat_id, prefix)
    if not path.exists():
        return []
    messages = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
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


def _save_history(chat_id: int, role: str, content: str, prefix: str = "") -> None:
    if role not in ("user", "assistant"):
        return
    path = _history_path(chat_id, prefix)
    with _history_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"role": role, "content": content, "ts": datetime.now().isoformat()}, ensure_ascii=False) + "\n")
    _trim_history(chat_id, prefix=prefix)


def _trim_history(chat_id: int, max_lines: int = 40, prefix: str = "") -> None:
    path = _history_path(chat_id, prefix)
    if not path.exists():
        return
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clear_history(chat_id: int, prefix: str = "") -> None:
    path = _history_path(chat_id, prefix)
    if path.exists():
        path.unlink()


def _truncate_messages(messages: list[dict]) -> list[dict]:
    total = 0
    result = []
    for msg in reversed(messages):
        total += len(msg.get("content") or "")
        if total > MAX_CHARS:
            break
        result.append(msg)
    return list(reversed(result))


def save_chat(user_id: int, role: str, content: str, prefix: str = ""):
    user_dir = get_user_dir(user_id, prefix)
    history_file = user_dir / "chat_history.jsonl"
    with open(history_file, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": role, "content": content, "ts": datetime.now().isoformat()}, ensure_ascii=False) + "\n")


def load_context(chat_id: int, limit: int = CHAT_HISTORY_LIMIT, prefix: str = "") -> str:
    user_dir = get_user_dir(chat_id, prefix)
    history_file = _history_path(chat_id, prefix)
    summary_file = user_dir / "summary.txt"
    context_parts = []
    if summary_file.exists():
        summary = summary_file.read_text(encoding="utf-8")
        context_parts.append(f"### [이전 대화 요약]\n{summary}\n")
    messages = _load_history(chat_id, prefix)
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
    final_messages: list[dict] = []
    if system_prompt:
        final_messages.append({"role": "system", "content": system_prompt})
    if messages:
        final_messages.extend(messages)
    user_msg: dict = {"role": "user", "content": prompt}
    if images:
        user_msg["images"] = images
    final_messages.append(user_msg)

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": final_messages,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"num_predict": max_tokens, "num_ctx": target_ctx},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["message"]["content"]


def _ask_llama_cpp(prompt: str, system_prompt: str = "", images: list | None = None,
                   max_tokens: int = 8192, timeout: int = 300, **kwargs) -> str:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if images:
        content = [{"type": "text", "text": prompt}]
        for img_b64 in images:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})

    body: dict = {"messages": messages, "max_tokens": max_tokens}
    if LLAMA_CPP_MODEL:
        body["model"] = LLAMA_CPP_MODEL
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{LLAMA_CPP_URL}/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = json.loads(resp.read())["choices"][0]["message"]["content"]
        text = re.sub(r'<think>[\s\S]*?</think>', '', text)
        return re.sub(r'<\|[^|]*\|>', '', text).strip()
    except urllib.request.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        log.error("llama-cpp %s: %s", e.code, body_err)
        raise


def _ask_unsloth(prompt: str, system_prompt: str = "", images: list | None = None,
                 max_tokens: int = 8192, timeout: int = 300, **kwargs) -> str:
    import os
    base_url = os.environ.get("UNSLOTH_BASE_URL", "http://127.0.0.1:8888/v1").rstrip("/")
    api_key = os.environ.get("UNSLOTH_API_KEY", "")
    model = os.environ.get("UNSLOTH_MODEL", "")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if images:
        content = [{"type": "text", "text": prompt}]
        for img_b64 in images:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})
    body: dict = {"messages": messages, "max_tokens": max_tokens}
    if model:
        body["model"] = model
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"{base_url}/chat/completions", data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = json.loads(resp.read())["choices"][0]["message"]["content"]
        text = re.sub(r'<think>[\s\S]*?</think>', '', text)
        return re.sub(r'<\|[^|]*\|>', '', text).strip()
    except urllib.request.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        log.error("unsloth %s: %s", e.code, body_err)
        raise


def ask_ollama(prompt: str, system_prompt: str = "", images: list | None = None,
               max_tokens: int = 8192, timeout: int = 300,
               num_ctx: int | None = None, keep_alive: str = "5m",
               messages: list[dict] | None = None) -> str:
    # 현재 시각 정보 주입
    now = datetime.now()
    time_info = f"\n\n[현재 시각 및 요건]\n- 일시: {now.strftime('%Y년 %m월 %d일 %A %H:%M:%S')}"
    system_prompt += time_info

    if MODEL_BACKEND == "unsloth":
        return _ask_unsloth(prompt, system_prompt, images, max_tokens, timeout)
    if MODEL_BACKEND == "llama-cpp":
        return _ask_llama_cpp(prompt, system_prompt, images, max_tokens, timeout,
                              num_ctx=num_ctx, keep_alive=keep_alive)

    if "Planner" in system_prompt or "JSON" in system_prompt:
        system_prompt += (
            "\n- 사용자가 '폴더', '파일', '경로', '디렉토리'를 언급하거나 로컬 환경 탐색 중이라면, "
            "절대로 웹 검색이 아닌 로컬 도구(read_file, list_dir 등)를 사용하도록 유도하십시오.\n"
            "- 확실하지 않은 경우 일반 지식으로 대답하지 말고 도구를 사용해 사실을 확인하십시오."
        )

    return _ask_ollama(prompt, system_prompt, images, max_tokens, timeout,
                       num_ctx=num_ctx, keep_alive=keep_alive, messages=messages)


# ── 파일시스템 툴 콜링 ────────────────────────────────────────────────────────

FS_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "파일 내용을 읽어서 반환. 코드, 설정, 로그 파일 확인에 사용.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "파일 절대 경로"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir", "description": "디렉토리 내 파일/폴더 목록 반환.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "디렉토리 절대 경로. 기본: /home/home"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "파일에 내용을 씀. 코드 수정, 메모 저장에 사용.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "파일 절대 경로"}, "content": {"type": "string", "description": "저장할 내용"}}, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "search_files", "description": "/home/home 아래에서 파일명 패턴으로 파일 검색.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string", "description": "검색 패턴 (예: *.py, bot_*.py, *.md)"}}, "required": ["pattern"]},
    }},
]


def _exec_tool(name: str, args: dict, allow_write: bool = False) -> str:
    try:
        if name == "read_file":
            path = Path(args["path"])
            if not path.resolve().is_relative_to(HOME_DIR):
                return "오류: /home/home 하위 경로만 접근 가능"
            if not path.exists():
                return f"오류: 파일 없음 — {path}"
            return path.read_text(encoding="utf-8", errors="replace")[:8000]
        elif name == "list_dir":
            path = Path(args.get("path", "/home/home"))
            if not path.resolve().is_relative_to(HOME_DIR):
                return "오류: /home/home 하위 경로만 접근 가능"
            if not path.exists():
                return f"오류: 디렉토리 없음 — {path}"
            lines = [f"{'📁' if item.is_dir() else '📄'} {item.name}" for item in sorted(path.iterdir())]
            return "\n".join(lines) or "(비어있음)"
        elif name == "write_file":
            if not allow_write:
                return "오류: 쓰기 권한 없음 (관리자만 가능)"
            path = Path(args["path"])
            if not path.resolve().is_relative_to(HOME_DIR):
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


def _openai_tools_loop(messages: list[dict], base_url: str, api_key: str,
                       model: str, allow_write: bool, max_iter: int = 8) -> str:
    """OpenAI-호환 서버용 동기 tool calling 루프."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    for _ in range(max_iter):
        body: dict = {"messages": messages, "tools": FS_TOOLS, "max_tokens": 4096}
        if model:
            body["model"] = model
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read())
        except urllib.request.HTTPError as e:
            log.error("openai tools %s: %s", e.code, e.read().decode("utf-8", errors="replace"))
            raise

        choices = result.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message", {}) or {}
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            content = msg.get("content") or ""
            content = re.sub(r'<think>[\s\S]*?</think>', '', content)
            return re.sub(r'<\|[^|]*\|>', '', content).strip()

        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            fn = tc.get("function", {}) or {}
            args_raw = fn.get("arguments", "{}")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw or {}
            tool_result = _exec_tool(fn.get("name", ""), args, allow_write)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": tool_result,
            })

    return messages[-1].get("content") or ""


def ask_ollama_with_tools(messages: list[dict], system_prompt: str = "",
                          allow_write: bool = False) -> str:
    final_messages: list[dict] = []
    if system_prompt:
        final_messages.append({"role": "system", "content": system_prompt})
    final_messages.extend(messages)

    if MODEL_BACKEND == "unsloth":
        import os
        return _openai_tools_loop(
            final_messages,
            base_url=os.environ.get("UNSLOTH_BASE_URL", "http://127.0.0.1:8888/v1").rstrip("/"),
            api_key=os.environ.get("UNSLOTH_API_KEY", ""),
            model=os.environ.get("UNSLOTH_MODEL", ""),
            allow_write=allow_write,
        )
    if MODEL_BACKEND == "llama-cpp":
        return _openai_tools_loop(
            final_messages,
            base_url=f"{LLAMA_CPP_URL}/v1",
            api_key="",
            model=LLAMA_CPP_MODEL,
            allow_write=allow_write,
        )

    for _ in range(8):
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

        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_result = _exec_tool(fn.get("name", ""), fn.get("arguments", {}), allow_write)
            final_messages.append({"role": "tool", "content": tool_result})

    return final_messages[-1].get("content", "")


# ── 대화 요약 ─────────────────────────────────────────────────────────────────

async def safe_summarize(chat_id: int, prefix: str = ""):
    async with _summary_lock:
        await check_and_summarize(chat_id, prefix)


async def check_and_summarize(chat_id: int, prefix: str = ""):
    user_dir = get_user_dir(chat_id, prefix)
    history_file = _history_path(chat_id, prefix)
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
            except json.JSONDecodeError:
                continue

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
