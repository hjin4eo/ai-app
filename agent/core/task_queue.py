#!/usr/bin/env python3
"""
task_queue.py — 파일시스템 기반 AI 태스크 큐
tasks/{pending,in_progress,completed,cancelled,failed}/<uuid>.json
"""
from __future__ import annotations

import fcntl
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

TASKS_DIR = Path(__file__).parent.parent.parent / "tasks"

STATES = ("pending", "in_progress", "completed", "cancelled", "failed")


def _dir(state: str) -> Path:
    d = TASKS_DIR / state
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_path() -> Path:
    return TASKS_DIR / ".lock"


class _Lock:
    def __enter__(self):
        self._f = open(_lock_path(), "w")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()


def create_task(description: str, source: str = "chat") -> dict:
    task_id = uuid.uuid4().hex[:8]
    task = {
        "id": task_id,
        "status": "pending",
        "created": datetime.now().isoformat(),
        "source": source,
        "description": description,
        "branch": f"ai/task-{task_id}",
        "retries": 0,
        "result": None,
    }
    with _Lock():
        (_dir("pending") / f"{task_id}.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2)
        )
    return task


def get_task(task_id: str) -> Optional[dict]:
    for state in STATES:
        p = _dir(state) / f"{task_id}.json"
        if p.exists():
            return json.loads(p.read_text())
    return None


def list_tasks(state: str = "pending") -> list[dict]:
    return [
        json.loads(p.read_text())
        for p in sorted(_dir(state).glob("*.json"))
    ]


def update_task(task_id: str, **kwargs) -> Optional[dict]:
    with _Lock():
        for state in STATES:
            p = _dir(state) / f"{task_id}.json"
            if p.exists():
                task = json.loads(p.read_text())
                new_state = kwargs.get("status", task["status"])
                task.update(kwargs)
                # 상태 변경 시 파일 이동
                if new_state != state:
                    p.unlink()
                    (_dir(new_state) / f"{task_id}.json").write_text(
                        json.dumps(task, ensure_ascii=False, indent=2)
                    )
                else:
                    p.write_text(json.dumps(task, ensure_ascii=False, indent=2))
                return task
    return None


def cancel_task(task_id: str) -> Optional[dict]:
    task = get_task(task_id)
    if not task:
        return None
    if task["status"] in ("completed", "cancelled", "failed"):
        return task
    return update_task(task_id, status="cancelled")


def pop_next_pending() -> Optional[dict]:
    """가장 오래된 pending 태스크를 in_progress로 전환 후 반환."""
    with _Lock():
        files = sorted(_dir("pending").glob("*.json"))
        if not files:
            return None
        task = json.loads(files[0].read_text())
        task["status"] = "in_progress"
        files[0].unlink()
        (_dir("in_progress") / files[0].name).write_text(
            json.dumps(task, ensure_ascii=False, indent=2)
        )
        return task
