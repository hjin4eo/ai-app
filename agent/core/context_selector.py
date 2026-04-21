"""
context_selector.py — Phase 2: 지능형 컨텍스트 선택기

plan_changes()가 반환한 파일 목록을 기반으로
AI에게 넘길 코드 컨텍스트를 최적 선택.

전략:
1. plan["files"]에 명시된 파일 우선 포함
2. import 그래프 BFS로 직접/간접 의존 파일 자동 발견 (Step 2)
3. 전체 크기 제한 내에서 관련도 순으로 채움 (직접 > 1hop > 2hop > 나머지)
"""
from __future__ import annotations

import ast
import logging
from collections import defaultdict, deque
from pathlib import Path

log = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 12_000
# import 그래프 BFS 최대 깊이 (직접=0, 1hop=1, 2hop=2)
MAX_HOP = 2


# ── import 그래프 빌더 ────────────────────────────────────────────────────────

def _parse_imports(source: str) -> list[str]:
    """Python 소스에서 import된 모듈명 목록 추출 (ast 파싱)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
    return modules


def _module_to_rel_path(module: str, work_dir: Path) -> str | None:
    """모듈명('core.bot_config') → 레포 내 상대경로('agent/core/bot_config.py').

    agent/ 패키지 하위를 우선 탐색, 없으면 work_dir 전체 탐색.
    """
    parts = module.split(".")
    candidates = [
        work_dir / Path(*parts).with_suffix(".py"),
        work_dir / "agent" / Path(*parts).with_suffix(".py"),
    ]
    for c in candidates:
        if c.exists():
            return str(c.relative_to(work_dir))
    # 모듈 이름 마지막 부분만으로 파일 검색 (패키지 접두사 다를 때)
    leaf = parts[-1] + ".py"
    for p in work_dir.rglob(leaf):
        try:
            return str(p.relative_to(work_dir))
        except ValueError:
            pass
    return None


def build_import_graph(work_dir: Path, protected_check) -> dict[str, set[str]]:
    """레포 내 Python 파일의 import 그래프 구축.

    Returns:
        {rel_path: {직접 import하는 rel_path, ...}}
    """
    graph: dict[str, set[str]] = defaultdict(set)
    for p in work_dir.rglob("*.py"):
        if protected_check(p):
            continue
        try:
            rel = str(p.relative_to(work_dir))
            source = p.read_text(errors="replace")
            for mod in _parse_imports(source):
                dep = _module_to_rel_path(mod, work_dir)
                if dep and dep != rel:
                    graph[rel].add(dep)
        except Exception as e:
            log.debug("import 파싱 실패 %s: %s", p, e)
    return dict(graph)


# ── BFS 관련 파일 탐색 ────────────────────────────────────────────────────────

def find_related(
    seed_files: list[str],
    graph: dict[str, set[str]],
    max_hop: int = MAX_HOP,
) -> list[tuple[int, str]]:
    """seed_files에서 BFS로 관련 파일 탐색.

    Returns:
        [(hop_distance, rel_path), ...] — hop 오름차순 (가까울수록 먼저)
    """
    visited: dict[str, int] = {}
    queue: deque[tuple[int, str]] = deque()

    for f in seed_files:
        if f not in visited:
            visited[f] = 0
            queue.append((0, f))

    while queue:
        hop, node = queue.popleft()
        if hop >= max_hop:
            continue
        for neighbor in graph.get(node, set()):
            if neighbor not in visited:
                visited[neighbor] = hop + 1
                queue.append((hop + 1, neighbor))

    # seed 파일은 제외 (이미 plan에서 포함됨)
    seed_set = set(seed_files)
    result = [(hop, f) for f, hop in visited.items() if f not in seed_set]
    result.sort(key=lambda x: x[0])
    return result


# ── 메인 API ─────────────────────────────────────────────────────────────────

def select_context(plan: dict, work_dir: Path, protected_check) -> str:
    """plan_changes() 결과를 토대로 코드 컨텍스트 문자열 생성.

    Args:
        plan: {"files": [...], "approach": "..."}
        work_dir: 레포 루트 경로
        protected_check: _is_protected(path) → bool

    Returns:
        AI에게 넘길 코드 컨텍스트 문자열
    """
    plan_files: list[str] = plan.get("files", [])
    approach: str = plan.get("approach", "")

    parts: list[str] = []
    included: set[str] = set()
    total = 0

    if approach:
        header = f"# 구현 계획\n{approach}\n"
        parts.append(header)
        total += len(header)

    def _add_file(rel_path: str) -> bool:
        nonlocal total
        if rel_path in included:
            return True
        p = work_dir / rel_path
        if not p.exists() or protected_check(p):
            return False
        try:
            content = p.read_text(errors="replace")
            chunk = f"### {rel_path}\n{content}"
            if total + len(chunk) > MAX_CONTEXT_CHARS:
                return False
            parts.append(chunk)
            included.add(rel_path)
            total += len(chunk)
            return True
        except Exception as e:
            log.warning("파일 읽기 실패 %s: %s", rel_path, e)
            return False

    # ── 1. plan 파일 우선 포함 ────────────────────────────────────────────
    for rel_path in plan_files:
        if not _add_file(rel_path):
            log.warning("plan 파일 없음 또는 한도 초과: %s", rel_path)

    # ── 2. import 그래프 BFS로 관련 파일 보충 ────────────────────────────
    if total < MAX_CONTEXT_CHARS:
        try:
            graph = build_import_graph(work_dir, protected_check)
            related = find_related(plan_files, graph, max_hop=MAX_HOP)
            for _hop, rel_path in related:
                if total >= MAX_CONTEXT_CHARS:
                    break
                _add_file(rel_path)
        except Exception as e:
            log.warning("import 그래프 분석 실패: %s", e)

    # ── 3. 한도 여유 있으면 나머지 파일 스캔으로 채움 ──────────────────
    if total < MAX_CONTEXT_CHARS // 2:
        for ext in (".py", ".js", ".ts"):
            for p in sorted(work_dir.rglob(f"*{ext}")):
                if total >= MAX_CONTEXT_CHARS:
                    break
                try:
                    rel = str(p.relative_to(work_dir))
                    _add_file(rel)
                except ValueError:
                    pass

    return "\n\n".join(parts)
