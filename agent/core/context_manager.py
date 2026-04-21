#!/usr/bin/env python3
"""
context_manager.py — SQLite 기반 대화 맥락 관리 모듈
"""
import asyncio
import sqlite3
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

from core.bot_config import DATA_DIR

log = logging.getLogger(__name__)

class ContextManager:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (DATA_DIR / "conversation.db")
        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self._init_db()

    def _init_db(self):
        """데이터베이스 테이블 초기화"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # 메시지 테이블 (최근 대화 윈도우용)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT,
                    role TEXT CHECK(role IN ('user', 'assistant', 'tool', 'system')),
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # 요약 테이블 (장기 기억 저장용)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    chat_id TEXT PRIMARY KEY,
                    summary TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()

    async def save_message(self, chat_id: int, role: str, content: str) -> None:
        """새로운 메시지를 데이터베이스에 안전하게 저장하고, 용량 초과시 오래된 메시지를 삭제합니다."""
        def _save():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # 1. 새 메시지 삽입 (파라미터화된 쿼리)
                cursor.execute(
                    'INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)',
                    (str(chat_id), role, content)
                )
                
                # 2. 데이터 보존 정책 (Pruning): 각 채팅당 최대 150개의 메시지만 유지
                # 150개를 초과하면 가장 오래된 것부터 삭제합니다.
                retention_limit = 150
                cursor.execute('''
                    DELETE FROM messages 
                    WHERE id IN (
                        SELECT id FROM messages 
                        WHERE chat_id = ? 
                        ORDER BY id DESC 
                        LIMIT -1 OFFSET ?
                    )
                ''', (str(chat_id), retention_limit))
                
                conn.commit()
                
        async with self.lock:
            await asyncio.to_thread(_save)

    async def get_context(self, chat_id: int, limit: int = 10) -> Dict[str, Any]:
        """
        요약본 + 최신 통합 메시지(시간순 정렬)를 반환합니다.
        limit: 가져올 최신 메시지의 총 개수 (기본 10개)
        """
        def _fetch():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 요약본 가져오기
                cursor.execute('SELECT summary FROM conversation_summaries WHERE chat_id = ?', (str(chat_id),))
                summary_row = cursor.fetchone()
                summary = summary_row[0] if summary_row else ""

                # 최근 메시지 가져오기 (시간 역순으로 limit개 가져와서 시간순으로 재정렬)
                cursor.execute('''
                    SELECT role, content FROM messages 
                    WHERE chat_id = ? 
                    ORDER BY id DESC LIMIT ?
                ''', (str(chat_id), limit))
                
                rows = cursor.fetchall()
                # 시간순 정렬 (최근 역순을 다시 역순)
                messages = [{"role": row[0], "content": row[1]} for row in reversed(rows)]
                
                return {
                    "summary": summary,
                    "messages": messages
                }
                
        async with self.lock:
            return await asyncio.to_thread(_fetch)

    async def update_summary(self, chat_id: int, new_summary: str) -> None:
        """대화 요약본을 갱신합니다. (Upsert)"""
        def _update():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO conversation_summaries (chat_id, summary, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET 
                        summary = excluded.summary,
                        updated_at = CURRENT_TIMESTAMP
                ''', (str(chat_id), new_summary))
                conn.commit()
                
        async with self.lock:
            await asyncio.to_thread(_update)

    async def get_summary(self, chat_id: int) -> str:
        """특정 채팅의 요약본을 가져옵니다."""
        def _fetch():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT summary FROM conversation_summaries WHERE chat_id = ?', (str(chat_id),))
                row = cursor.fetchone()
                return row[0] if row else ""
                
        async with self.lock:
            return await asyncio.to_thread(_fetch)

    async def get_recent_messages(self, chat_id: int, limit: int = 30) -> List[Dict[str, str]]:
        """요약을 위해 특정 개수만큼의 최근 메시지를 가져옵니다."""
        def _fetch():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT role, content FROM messages 
                    WHERE chat_id = ? 
                    ORDER BY id ASC LIMIT ?
                ''', (str(chat_id), limit))
                rows = cursor.fetchall()
                return [{"role": row[0], "content": row[1]} for row in rows]
                
        async with self.lock:
            return await asyncio.to_thread(_fetch)

    async def delete_oldest_messages(self, chat_id: int, count: int) -> None:
        """요약 완료 후 이미 요약된 가장 오래된 메시지 N개를 삭제합니다."""
        def _delete():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    DELETE FROM messages 
                    WHERE id IN (
                        SELECT id FROM messages 
                        WHERE chat_id = ? 
                        ORDER BY id ASC 
                        LIMIT ?
                    )
                ''', (str(chat_id), count))
                conn.commit()
                
        async with self.lock:
            await asyncio.to_thread(_delete)

    async def get_message_count(self, chat_id: int) -> int:
        """특정 채팅의 전체 메시지 개수를 가져옵니다."""
        def _count():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM messages WHERE chat_id = ?', (str(chat_id),))
                row = cursor.fetchone()
                return row[0] if row else 0
                
        async with self.lock:
            return await asyncio.to_thread(_count)

    async def clear_history(self, chat_id: int) -> None:
        """특정 채팅의 기록과 요약을 삭제합니다."""
        def _clear():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM messages WHERE chat_id = ?', (str(chat_id),))
                cursor.execute('DELETE FROM conversation_summaries WHERE chat_id = ?', (str(chat_id),))
                conn.commit()
                
        async with self.lock:
            await asyncio.to_thread(_clear)

# 싱글톤 인스턴스 (앱 전체에서 공유)
context_manager_db = ContextManager()
