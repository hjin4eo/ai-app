#!/usr/bin/env python3
"""
bot_utils.py — Text utilities, authentication, chat history management, and model interaction.
Provides core utility functions for the Telegram bot, including async model calls and secure file operations.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import aiohttp
from telegram import Update
from telegram.helpers import escape_markdown

from core.bot_config import (
    ALLOWED_CHAT_IDS,
    DATA_DIR,
    LLAMA_CPP_URL,
    LLAMA_CPP_MODEL,
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

from core.context_manager import context_manager_db

# ── Global Settings & Constants ───────────────────────────────────────────
HISTORY_DIR = Path(__file__).parent.parent.parent / ".history"
MAX_HISTORY = 20       # Recent 20 turns (40 messages)
MAX_CHARS   = 12000    # Total character limit for messages (to prevent context overflow)

CHAT_SYSTEM_PROMPT = (
    "너는 도움이 되는 AI 어시스턴트야. 한국어로 대답해.\n"
    "답변은 1000자 이내로 작성해. 핵심만 간결하게, 불필요한 부연설명은 생략해.\n\n"
    + TELEGRAM_FORMAT_RULES
)
SEARCH_SYSTEM_PROMPT = (
    "너는 웹 검색 결과를 요약하는 AI야. 한국어로 답해.\n\n"
    + TELEGRAM_FORMAT_RULES
)

# Locks for thread-safe/async-safe operations
_summary_lock = asyncio.Lock()

log = logging.getLogger(__name__)

# ── Text Utilities ────────────────────────────────────────────────────────

def is_local_query(text: str) -> bool:
    """
    Determines if the query is related to local files or paths.
    """
    if len(text) < 5:
        return False
    keywords = ["폴더", "파일", "경로", "디렉토리", "소스", "코드", "수정", "탐색"]
    has_keyword = any(k in text for k in keywords)
    has_path = "/" in text or "\\" in text
    return has_keyword or has_path

def escape_safe(text: str) -> str:
    """
    Escapes text for Telegram Markdown V2 while preserving code blocks.
    """
    parts = text.split("```")
    for i in range(len(parts)):
        if i % 2 == 0:  # Only escape non-code block parts
            parts[i] = escape_markdown(parts[i], version=2)
    return "```".join(parts)

def _strip_tags(text: str) -> str:
    """
    Forcibly removes HTML tags and semi-common Markdown markers for clean display.
    """
    text = re.sub(r'<[^>]+>', '', text)           # Remove all HTML tags
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group().strip('`'), text)  # Strip code block backticks
    text = re.sub(r'(?<!\w)\*{1,3}([^*]+)\*{1,3}(?!\w)', r'\1', text)      # Remove bold/italic stars
    text = re.sub(r'(?<!\w)_{1,3}([^_]+)_{1,3}(?!\w)', r'\1', text)        # Remove italic underscores
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)             # Remove headers
    return text.strip()

def _escape_html(text: str) -> str:
    """
    Escapes special characters for safe use with HTML parse_mode.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _strip_markup(text: str) -> str:
    """
    Removes HTML tags and common Markdown formatting to return pure text.
    Used for processing AI responses before further transformation.
    """
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'#{1,6}\s*', '', text)
    # Table processing
    text = re.sub(r'^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\|(.+)\|$', lambda m: '  '.join(cell.strip() for cell in m.group(1).split('|') if cell.strip()), text, flags=re.MULTILINE)
    # Generic markdown
    text = re.sub(r'^\s*[-*+]\s+', '- ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'~{2}(.+?)~{2}', r'\1', text)
    # Clean up empty lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def _sanitize_html(text: str) -> str:
    """
    Removes HTML tags not supported by Telegram.
    """
    allowed = {'b', 'i', 'code', 'pre', 'a', 's', 'u'}
    def _replace_tag(m):
        tag_match = re.match(r'</?(\w+)', m.group(0))
        if tag_match and tag_match.group(1).lower() in allowed:
            return m.group(0)
        return ''
    text = re.sub(r'</?[a-zA-Z][^>]*>', _replace_tag, text)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    return text

async def _send_long(update: Update, text: str, parse_mode: str = None) -> None:
    """
    Splits long messages into 4000-character chunks and sends them.
    """
    for i in range(0, len(text), 4000):
        chunk = text[i : i + 4000]
        try:
            await update.message.reply_text(chunk, parse_mode=parse_mode)
        except Exception as e:
            log.warning(f"Failed to send with parse_mode={parse_mode}, falling back to plain text: {e}")
            await update.message.reply_text(chunk, parse_mode=None)

# ── Authentication ────────────────────────────────────────────────────────

def _admin(update: Update) -> bool:
    """
    Checks if the user is the primary admin.
    """
    return update.effective_chat.id == TELEGRAM_CHAT_ID

async def _auth(update: Update) -> bool:
    """
    Checks if the user is authorized (Admin or allowed users).
    Sends a notification to the admin if an unauthorized attempt is detected.
    """
    chat_id = update.effective_chat.id
    if chat_id in ALLOWED_CHAT_IDS:
        return True

    user = update.effective_user
    log.warning(f"Auth failed: chat_id={chat_id} user={user.full_name if user else '?'}")
    
    today = datetime.now().strftime("%Y-%m-%d")
    if _auth_fail_notified.get(chat_id) != today:
        _auth_fail_notified[chat_id] = today
        name = user.full_name if user else "Unknown"
        username = f"@{user.username}" if user and user.username else "None"
        
        msg = (
            f"🔒 *Unauthorized Access Attempt*\n\n"
            f"Chat ID: `{chat_id}`\n"
            f"Name: {name}\n"
            f"Username: {username}\n\n"
            f"To allow access:\n`/allow {chat_id}`"
        )
        
        # Async notification via Telegram API
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=5) as resp:
                    if resp.status != 200:
                        log.error(f"Failed to notify admin: {resp.status}")
        except Exception as e:
            log.error(f"Error during admin notification: {e}")
            
    return False

# ── History & Context Management ──────────────────────────────────────────

def get_user_dir(chat_id: int) -> Path:
    """Returns the dedicated data directory for a specific chat ID (still used for allowed_users etc.)."""
    user_dir = DATA_DIR / str(chat_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir

async def _save_history(chat_id: int, role: str, content: str) -> None:
    """Saves a message to SQLite history."""
    if role not in ("user", "assistant", "tool", "system"):
        return
    await context_manager_db.save_message(chat_id, role, content)

def _clear_history(chat_id: int) -> None:
    """Deletes the conversation history and summary from DB."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(context_manager_db.clear_history(chat_id))
    except RuntimeError:
        asyncio.run(context_manager_db.clear_history(chat_id))

def _truncate_messages(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Truncates messages from the beginning if total length exceeds MAX_CHARS.
    """
    total = 0
    result = []
    for msg in reversed(messages):
        total += len(msg.get("content") or "")
        if total > MAX_CHARS:
            break
        result.append(msg)
    return list(reversed(result))

async def save_chat(user_id: int, role: str, content: str):
    """Saves a chat message asynchronously."""
    await _save_history(user_id, role, content)

async def load_context(chat_id: int, limit: int = 10) -> Dict[str, Any]:
    """
    Constructs the conversation context including summary and recent history directly from SQLite.
    Returns: {"summary": str, "messages": List[Dict]}
    """
    return await context_manager_db.get_context(chat_id, limit=limit)


# ── AI Model Calling (Async) ──────────────────────────────────────────────

async def _ask_ollama(prompt: str, system_prompt: str = "", images: Optional[List[str]] = None,
                     max_tokens: int = 8192, timeout: int = 300,
                     num_ctx: Optional[int] = None, keep_alive: str = "5m",
                     messages: Optional[List[Dict[str, str]]] = None) -> str:
    """
    Sends a chat request to the Ollama server asynchronously.
    """
    target_ctx = num_ctx if num_ctx else OLLAMA_NUM_CTX
    final_messages: List[Dict[str, Any]] = []
    
    if system_prompt:
        final_messages.append({"role": "system", "content": system_prompt})
    if messages:
        final_messages.extend(messages)
        
    user_msg: Dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        user_msg["images"] = images
    final_messages.append(user_msg)

    payload = {
        "model": OLLAMA_MODEL,
        "messages": final_messages,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {
            "num_predict": max_tokens,
            "num_ctx": target_ctx,
        },
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=timeout) as resp:
            if resp.status != 200:
                error_body = await resp.text()
                log.error(f"Ollama error {resp.status}: {error_body}")
                raise Exception(f"Ollama server returned status {resp.status}")
            data = await resp.json()
            return data["message"]["content"]

async def _ask_llama_cpp(prompt: str, system_prompt: str = "", images: Optional[List[str]] = None,
                        max_tokens: int = 8192, timeout: int = 300,
                        history: Optional[List[Dict[str, str]]] = None, **kwargs) -> str:
    """
    Sends a chat request to the llama-cpp-python server asynchronously.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    # 대화 히스토리 삽입
    if history:
        messages.extend(history)

    if images:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img_b64 in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            })
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})

    import re
    payload: Dict[str, Any] = {"messages": messages, "max_tokens": max_tokens}
    if LLAMA_CPP_MODEL:
        payload["model"] = LLAMA_CPP_MODEL

    async with aiohttp.ClientSession() as session:
        async with session.post(f"{LLAMA_CPP_URL}/v1/chat/completions", json=payload, timeout=timeout) as resp:
            if resp.status != 200:
                error_body = await resp.text()
                log.error(f"llama-cpp error {resp.status}: {error_body}")
                raise Exception(f"llama-cpp server returned status {resp.status}")
            data = await resp.json()
            text = data["choices"][0]["message"]["content"]
    text = re.sub(r'<think>[\s\S]*?</think>', '', text)
    return re.sub(r'<\|[^|]*\|>', '', text).strip()

async def _ask_unsloth(prompt: str, system_prompt: str = "", images: Optional[List[str]] = None,
                      max_tokens: int = 8192, timeout: int = 300,
                      history: Optional[List[Dict[str, str]]] = None, **kwargs) -> str:
    import os, re
    base_url = os.environ.get("UNSLOTH_BASE_URL", "http://127.0.0.1:8888/v1").rstrip("/")
    api_key = os.environ.get("UNSLOTH_API_KEY", "")
    model = os.environ.get("UNSLOTH_MODEL", "")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history:
        messages.extend(history)
    if images:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img_b64 in images:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})
    payload: Dict[str, Any] = {"messages": messages, "max_tokens": max_tokens}
    if model:
        payload["model"] = model
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base_url}/chat/completions", json=payload,
                                headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                error_body = await resp.text()
                log.error(f"unsloth error {resp.status}: {error_body}")
                raise Exception(f"Unsloth Studio returned status {resp.status}")
            data = await resp.json()
            text = data["choices"][0]["message"]["content"]
    text = re.sub(r'<think>[\s\S]*?</think>', '', text)
    return re.sub(r'<\|[^|]*\|>', '', text).strip()


async def ask_ollama(prompt: str, system_prompt: str = "", images: Optional[List[str]] = None,
                    max_tokens: int = 8192, timeout: int = 300,
                    num_ctx: Optional[int] = None, keep_alive: str = "5m",
                    messages: Optional[List[Dict[str, str]]] = None) -> str:
    """
    Main entry point for model interaction. Automatically chooses backend.
    """
    if MODEL_BACKEND == "unsloth":
        return await _ask_unsloth(prompt, system_prompt, images, max_tokens, timeout,
                                  history=messages)
    if MODEL_BACKEND == "llama-cpp":
        return await _ask_llama_cpp(prompt, system_prompt, images, max_tokens, timeout,
                                    history=messages, num_ctx=num_ctx, keep_alive=keep_alive)
    
    # Enhanced system prompt for planning/local queries
    if any(k in system_prompt for k in ["Planner", "JSON"]):
        system_prompt += (
            "\n[중요 지침]\n"
            "- 사용자가 '폴더', '파일', '경로', '디렉토리'를 언급하거나 로컬 환경 탐색 중이라면, "
            "절대로 웹 검색이 아닌 로컬 도구(read_file, list_dir 등)를 사용하도록 유도하십시오.\n"
            "- 확실하지 않은 경우 일반 지식으로 대답하지 말고 도구를 사용해 사실을 확인하십시오."
        )

    return await _ask_ollama(prompt, system_prompt, images, max_tokens, timeout,
                            num_ctx=num_ctx, keep_alive=keep_alive, messages=messages)

# ── Filesystem Tool Execution ─────────────────────────────────────────────

HOME_DIR = Path("/home/home")

FS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file content. Useful for checking logs, code, or settings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories within a given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute directory path. Default: /home/home"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite file content. Use with caution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Recursively search for files matching a pattern under /home/home.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. *.py, config.yaml)"}
                },
                "required": ["pattern"]
            }
        }
    }
]

async def _exec_tool(name: str, args: Dict[str, Any], allow_write: bool = False) -> str:
    """Executes filesystem tool logic. Restricts access to /home/home subdirectory."""
    try:
        if name == "read_file":
            path = Path(args["path"])
            if not path.resolve().is_relative_to(HOME_DIR):
                return "Error: Access denied (only /home/home allowed)"
            if not await asyncio.to_thread(path.exists):
                return f"Error: File not found - {path}"
            content = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
            return content[:8000]

        elif name == "list_dir":
            path = Path(args.get("path", "/home/home"))
            if not path.resolve().is_relative_to(HOME_DIR):
                return "Error: Access denied (only /home/home allowed)"
            if not await asyncio.to_thread(path.exists):
                return f"Error: Directory not found - {path}"
            
            items = await asyncio.to_thread(lambda: sorted(path.iterdir()))
            lines = []
            for item in items:
                prefix = "📁" if item.is_dir() else "📄"
                lines.append(f"{prefix} {item.name}")
            return "\n".join(lines) or "(Empty)"

        elif name == "write_file":
            if not allow_write:
                return "Error: Write permission denied (Admin only)"
            path = Path(args["path"])
            if not path.resolve().is_relative_to(HOME_DIR):
                return "Error: Access denied (only /home/home allowed)"
            
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, args["content"], encoding="utf-8")
            return f"✅ Saved successfully: {path}"

        elif name == "search_files":
            pattern = args.get("pattern", "*")
            # Using rglob in a thread per recursive scan
            def _find():
                results = list(HOME_DIR.rglob(pattern))
                return [str(p) for p in results if ".git" not in p.parts][:20]
            
            results = await asyncio.to_thread(_find)
            if not results:
                return "No matching files found."
            return "\n".join(results)

        return f"Unknown tool: {name}"
    except Exception as e:
        log.error(f"Tool execution error: {e}")
        return f"Tool Error: {e}"

async def ask_ollama_with_tools(messages: List[Dict[str, str]], system_prompt: str = "",
                                allow_write: bool = False) -> str:
    """
    Tool calling loop. LM Studio 백엔드 시 tool call 없이 일반 채팅으로 폴백.
    """
    final_messages: List[Dict[str, Any]] = []
    if system_prompt:
        final_messages.append({"role": "system", "content": system_prompt})
    final_messages.extend(messages)

    # Ollama tool call 형식 미지원 백엔드 → 일반 채팅으로 폴백
    if MODEL_BACKEND == "llama-cpp":
        prompt = final_messages[-1].get("content", "") if final_messages else ""
        return await _ask_llama_cpp(prompt, system_prompt)
    if MODEL_BACKEND == "unsloth":
        prompt = final_messages[-1].get("content", "") if final_messages else ""
        return await _ask_unsloth(prompt, system_prompt)

    async with aiohttp.ClientSession() as session:
        for _ in range(8):
            payload = {
                "model": OLLAMA_MODEL,
                "messages": final_messages,
                "tools": FS_TOOLS,
                "stream": False,
                "options": {"num_ctx": OLLAMA_NUM_CTX},
            }

            async with session.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300) as resp:
                result = await resp.json()

            msg = result["message"]
            final_messages.append(msg)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return msg.get("content", "")

            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_result = await _exec_tool(fn.get("name", ""), fn.get("arguments", {}), allow_write)
                final_messages.append({"role": "tool", "content": tool_result})

    return final_messages[-1].get("content", "")

# ── Automated Summarization ───────────────────────────────────────────────

async def safe_summarize(chat_id: int) -> None:
    """Lock-protected summarization to prevent concurrent summary tasks."""
    async with _summary_lock:
        await check_and_summarize(chat_id)

async def check_and_summarize(chat_id: int) -> None:
    """
    Analyzes conversation density and generates a summary if threshold reached.
    Uses SQLite database instead of text files. Target 20 messages per summary cycle.
    """
    msg_count = await context_manager_db.get_message_count(chat_id)
    
    # 임계값: DB에 저장된 메시지가 30개 이상일 때 요약을 실행 (최신 10개만 남기고 20개를 요약)
    if msg_count >= 30:
        log.info(f"Chat {chat_id}: SQLite History threshold ({msg_count}) reached. Summarizing...")
        
        # 가장 오래된 20개 메시지를 요약 대상으로 가져옵니다.
        messages_to_summarize = await context_manager_db.get_recent_messages(chat_id, limit=20)
        
        old_text = ""
        for data in messages_to_summarize:
            old_text += f"{data['role']}: {data['content']}\n"
            
        existing_summary = await context_manager_db.get_summary(chat_id)
        if not existing_summary:
            existing_summary = "None"

        prompt = (
            f"Please update the summary of this conversation based on the existing summary and new interactions.\n\n"
            f"[Existing Summary]\n{existing_summary}\n\n"
            f"[New Messages]\n{old_text}\n\n"
            f"Provide a concise summary in Korean. Ensure it captures the core context effectively."
        )

        try:
            # Generate the new summary
            new_summary = await ask_ollama(prompt)
            # Update DB with new summary Let context_manager Upsert handle it
            await context_manager_db.update_summary(chat_id, new_summary)
            # Pruning: 요약이 끝난 가장 오래된 메시지 20개를 삭제하여 DB 용량 최적화
            await context_manager_db.delete_oldest_messages(chat_id, count=20)
            
            log.info(f"Chat {chat_id}: Summarization complete & pruned 20 messages.")
        except Exception as e:
            log.error(f"Summarization error: {e}")

