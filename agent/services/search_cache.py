#!/usr/bin/env python3
"""
검색 결과 캐시 (SQLite)
Telegram·Discord 봇 공통 사용.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from core.bot_config import DATA_DIR, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
CACHE_TTL = 172800  # 48 hours

# ── DB Initialization ─────────────────────────────────────────────────────────
_db_path = DATA_DIR / "search_cache.db"
_db_path.parent.mkdir(parents=True, exist_ok=True)

_conn = sqlite3.connect(str(_db_path), check_same_thread=False)
_conn.execute("""
    CREATE TABLE IF NOT EXISTS cache (
        key   TEXT PRIMARY KEY,
        query TEXT,
        result TEXT,
        ts    REAL,
        hit_count INTEGER DEFAULT 0
    )
""")
# hit_count 컬럼 마이그레이션 (기존 DB 대처)
try:
    _conn.execute("ALTER TABLE cache ADD COLUMN hit_count INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass  # 이미 컬럼이 존재함
_conn.commit()


def _cache_key(query: str) -> str:
    normalized = query.strip().lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ── 네이버 검색 ──────────────────────────────────────────────────────────────
def search_naver(query: str, max_results: int = 5) -> str:
    """네이버 웹 검색. 캐시 히트 시 API 호출 생략."""
    if not query.strip():
        return "검색어를 입력해 주세요."

    key = _cache_key(query)

    # 캐시 조회
    row = _conn.execute(
        "SELECT result, ts, hit_count FROM cache WHERE key = ?", (key,)
    ).fetchone()
    if row:
        res, last_ts, hits = row
        is_long_term = (hits >= 5)
        # 장기 기억은 30일(2592000초), 일반은 CACHE_TTL(48시간) 적용
        current_ttl = 2592000 if is_long_term else CACHE_TTL
        
        if res and (time.time() - last_ts) < current_ttl:
            # Hit! 카운트 및 시간 갱신
            _conn.execute(
                "UPDATE cache SET hit_count = hit_count + 1, ts = ? WHERE key = ?",
                (time.time(), key),
            )
            _conn.commit()
            return res

    # API 호출
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return "Naver API 키가 설정되지 않았습니다. (.env 파일을 확인하세요)"

    url = (
        f"https://openapi.naver.com/v1/search/webkr.json"
        f"?query={urllib.parse.quote(query)}&display={max_results}"
    )
    req = urllib.request.Request(url)
    req.add_header("X-Naver-Client-Id", NAVER_CLIENT_ID)
    req.add_header("X-Naver-Client-Secret", NAVER_CLIENT_SECRET)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return f"검색 엔진 통신 중 오류 발생: {e}"

    results = []
    for item in data.get("items", []):
        title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
        desc = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()
        link = item.get("link", "")
        results.append(f"- {title}\n  {desc}\n  {link}")
    result_text = "\n".join(results) if results else "검색 결과 없음"

    # 캐시 저장
    hit_count_to_save = (hits + 1) if row else 1
    _conn.execute(
        "INSERT OR REPLACE INTO cache (key, query, result, ts, hit_count) VALUES (?, ?, ?, ?, ?)",
        (key, query.strip(), result_text, time.time(), hit_count_to_save),
    )
    _conn.commit()

    return result_text


def clear_expired():
    """만료된 캐시 관리."""
    now = time.time()
    # 1. 단기 기억 (조회 5회 미만) 삭제: 48시간 기준
    _conn.execute(
        "DELETE FROM cache WHERE hit_count < 5 AND ts < ?", 
        (now - CACHE_TTL,)
    )
    
    # 2. 장기 기억 (조회 5회 이상) 내용 비우기: 30일 기준
    # 키워드는 남기되 결과 텍스트만 삭제하여 '키워드 인덱스'처럼 보관
    _conn.execute(
        "UPDATE cache SET result = '' WHERE hit_count >= 5 AND ts < ?", 
        (now - 2592000,)
    )
    _conn.commit()
