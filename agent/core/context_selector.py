"""
context_selector.py — Phase 2: 지능형 컨텍스트 선택기

plan_changes()가 반환한 파일 목록을 기반으로
AI에게 넘길 코드 컨텍스트를 최적 선택.

전략:
1. plan["files"]에 명시된 파일 우선 포함
2. 심볼 분석으로 직접 import하는 파일 추가 (TODO)
3. 전체 크기 제한 내에서 관련도 순으로 채움

현재 Phase 2 Step 1: 기본 골격 + plan 기반 선택 구현
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# 컨텍스트 총 문자 제한 (generate_patch 호출 전)
MAX_CONTEXT_CHARS = 12_000


def select_context(plan: dict, work_dir: Path, protected_check) -> str:
    """plan_changes() 결과를 토대로 코드 컨텍스트 문자열 생성.

    Args:
        plan: {"files": [...], "approach": "..."}
        work_dir: 레포 루트 경로
        protected_check: _is_protected(path) 함수

    Returns:
        AI에게 넘길 코드 컨텍스트 문자열
    """
    plan_files: list[str] = plan.get("files", [])
    approach: str = plan.get("approach", "")

    parts: list[str] = []
    total = 0

    if approach:
        header = f"# 구현 계획\n{approach}\n"
        parts.append(header)
        total += len(header)

    # plan에 명시된 파일 우선 포함
    for rel_path in plan_files:
        p = work_dir / rel_path
        if not p.exists():
            log.warning("plan 파일 없음: %s", rel_path)
            continue
        if protected_check(p):
            continue
        try:
            content = p.read_text(errors="replace")
            chunk = f"### {rel_path}\n{content}"
            if total + len(chunk) > MAX_CONTEXT_CHARS:
                break
            parts.append(chunk)
            total += len(chunk)
        except Exception as e:
            log.warning("파일 읽기 실패 %s: %s", rel_path, e)

    # plan 파일만으로 컨텍스트가 부족하면 전체 스캔으로 보충 (TODO: 심볼 분석 기반으로 개선)
    if total < MAX_CONTEXT_CHARS // 2:
        # TODO: Phase 2 Step 2 — import 그래프 분석으로 관련 파일 자동 발견
        _fill_from_scan(parts, plan_files, work_dir, protected_check, total)

    return "\n\n".join(parts)


def _fill_from_scan(
    parts: list[str],
    already_included: list[str],
    work_dir: Path,
    protected_check,
    current_total: int,
) -> None:
    """스캔 기반 보충 — plan에 없는 파일을 크기 한도 내에서 추가."""
    # TODO: Phase 2 Step 2 — 단순 스캔 대신 심볼/import 분석으로 관련도 계산
    extensions = {".py", ".js", ".ts"}
    total = current_total
    for ext in extensions:
        for p in sorted(work_dir.rglob(f"*{ext}")):
            if protected_check(p):
                continue
            rel = str(p.relative_to(work_dir))
            if rel in already_included:
                continue
            try:
                content = p.read_text(errors="replace")
                chunk = f"### {rel}\n{content}"
                if total + len(chunk) > MAX_CONTEXT_CHARS:
                    return
                parts.append(chunk)
                total += len(chunk)
            except Exception:
                pass
