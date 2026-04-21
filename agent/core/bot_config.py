#!/usr/bin/env python3
"""
bot_config.py — 설정 로드, 상수, 공유 mutable 상태
의존성 없음 (최하단 레이어)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── 설정 로드 ──────────────────────────────────────────────────────────────────
_base_dir = Path(__file__).parent.parent.parent
_cfg_path = _base_dir / "config.yaml"
with open(_cfg_path) as f:
    CFG = yaml.safe_load(f)

_env_file = _base_dir / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, v = _line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ── 상수 ──────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = (
    CFG.get("telegram", {}).get("bot_token")
    or os.environ.get("TELEGRAM_BOT_TOKEN", "")
)
TELEGRAM_CHAT_ID: int = int(
    CFG.get("telegram", {}).get("chat_id")
    or os.environ.get("TELEGRAM_CHAT_ID", "0")
)

# Initialize with the main admin ID
ALLOWED_CHAT_IDS: set[int] = {TELEGRAM_CHAT_ID}

def load_allowed_users():
    """Loads allowed users from config, environment, and persistent data storage."""
    global ALLOWED_CHAT_IDS
    ALLOWED_CHAT_IDS.add(TELEGRAM_CHAT_ID)
    
    # 1. Load from config/env
    _raw_allowed = (
        CFG.get("telegram", {}).get("allowed_chat_ids")
        or os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    )
    for _id in str(_raw_allowed).split(","):
        _id = _id.strip()
        if _id:
            try:
                ALLOWED_CHAT_IDS.add(int(_id))
            except ValueError:
                pass

    # 2. Load from persistent dynamic file
    allowed_json = DATA_DIR / "allowed_users.json"
    if allowed_json.exists():
        try:
            import json
            dynamic_ids = json.loads(allowed_json.read_text())
            if isinstance(dynamic_ids, list):
                for _id in dynamic_ids:
                    ALLOWED_CHAT_IDS.add(int(_id))
        except Exception as e:
            log.warning(f"Failed to load dynamic allowed users: {e}")

DATA_DIR: Path = _base_dir / "data"

# Initial load
load_allowed_users()
OLLAMA_URL: str = CFG.get("ollama", {}).get("url", "http://localhost:11434")
LLAMA_CPP_URL_BASE: str = CFG.get("llama_cpp", {}).get("url", "http://localhost:1234")

OLLAMA_MODEL: str = (
    CFG.get("telegram", {}).get("ollama_model")
    or CFG.get("agent", {}).get("ollama_model", "gemma4:e4b-it-q8_0")
)
LLAMA_CPP_URL: str = CFG.get("llama_cpp", {}).get("url", "http://localhost:1234")
LLAMA_CPP_MODEL: str = CFG.get("llama_cpp", {}).get("model", "")

# .env의 MODEL_BACKEND가 config.yaml보다 우선
MODEL_BACKEND: str = (
    os.environ.get("MODEL_BACKEND")
    or CFG.get("agent", {}).get("model", "ollama")
)  # ollama / llama-cpp

# .env의 EMBEDDING_BACKEND가 config.yaml보다 우선
EMBEDDING_BACKEND: str = (
    os.environ.get("EMBEDDING_BACKEND")
    or CFG.get("agent", {}).get("embedding_backend", "ollama")
)
EMBEDDING_URL: str = OLLAMA_URL if EMBEDDING_BACKEND == "ollama" else LLAMA_CPP_URL_BASE
RUNNER_PATTERN: str = CFG.get("github_runner", {}).get("service_pattern", "")
NAVER_CLIENT_ID: str = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET: str = os.environ.get("NAVER_CLIENT_SECRET", "")

OLLAMA_NUM_CTX: int = int(CFG.get("agent", {}).get("num_ctx", 16384))
VISION_NUM_CTX: int = int(CFG.get("agent", {}).get("vision_num_ctx", 6000))
CHAT_HISTORY_LIMIT: int = int(CFG.get("agent", {}).get("chat_history_limit", 30))
SEARCH_RESULT_LIMIT: int = int(CFG.get("agent", {}).get("search_result_limit", 3000))

RAG_ENABLED: bool = bool(CFG.get("agent", {}).get("rag_enabled", False))
CHUNK_SIZE: int = int(CFG.get("agent", {}).get("chunk_size", 512))
CHUNK_OVERLAP: int = int(CFG.get("agent", {}).get("chunk_overlap", 50))
EMBEDDING_MODEL: str = CFG.get("agent", {}).get("embedding_model", "nomic-embed-text")
KNOWLEDGE_DIR: Path = _base_dir / CFG.get("agent", {}).get("knowledge_dir", "knowledge")

# ── 포맷 규칙 ─────────────────────────────────────────────────────────────────
TELEGRAM_FORMAT_RULES = (
    "[출력 형식]\n"
    "- 가급적 순수 텍스트를 사용하되, 코드나 디렉토리 구조처럼 구조화된 정보는 마크다운(```)을 사용하여 가독성을 높여줘.\n"
    "- 강조가 필요한 핵심 항목 앞에 이모지를 1개 사용해. (예: ✅ 결론, ⚠️ 주의)\n"
    "- 답변은 간결하고 명확하게, 핵심 위주로 작성해.\n"
    "- 답변 끝에 불필요한 인사말이나 안내 문구는 생략해."
)

# ── 공유 mutable 상태 ─────────────────────────────────────────────────────────

# ── 공유 mutable 상태 ─────────────────────────────────────────────────────────
_auth_fail_notified: dict[int, str] = {}  # chat_id → 마지막 알림 날짜 (YYYY-MM-DD)
