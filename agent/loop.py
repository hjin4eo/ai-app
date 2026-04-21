#!/usr/bin/env python3
"""
AI 피드백 루프
- GitHub Actions 실패 감지 → AI 분석 → 코드 수정 → push → 반복
- tasks/pending/ 폴링 → AI 코드 생성 → ai/task-<id> 브랜치 → push
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import yaml
from github import Github, GithubException

_agent_dir = Path(__file__).parent.resolve()
if str(_agent_dir) not in sys.path:
    sys.path.append(str(_agent_dir))

from core.bot_config import CFG
from core.context_selector import select_context
from core.models import get_model
from core.task_queue import pop_next_pending, update_task, list_tasks, create_task

POLL_INTERVAL = int(CFG["github"]["poll_interval"])
MAX_RETRIES = int(CFG["agent"]["max_retries"])
WORK_DIR = Path(CFG["agent"].get("work_dir", ".")).resolve()

# AI 태스크가 절대 수정할 수 없는 파일 (WORK_DIR 기준 상대경로)
PROTECTED = {
    "agent/loop.py",
    "config.yaml",
    ".env",
    "agent/core/task_queue.py",
    "agent/core/bot_config.py",
    "agent/core/models.py",
}
PROTECTED_DIRS = {"venv", ".venv", "node_modules", ".git", "tasks"}

log = logging.getLogger(__name__)


def _is_protected(path: Path) -> bool:
    try:
        rel = str(path.relative_to(WORK_DIR))
    except ValueError:
        rel = str(path)
    if rel in PROTECTED:
        return True
    if any(part in PROTECTED_DIRS for part in path.parts):
        return True
    return False


def _notify_photo(image_path: str, caption: str) -> None:
    """Telegram으로 이미지(스크린샷) 전송."""
    try:
        import urllib.request, urllib.parse, json as _json
        token = os.environ.get("LOG_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("LOG_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id or not Path(image_path).exists():
            return
        boundary = "----boundary"
        with open(image_path, "rb") as f:
            img_data = f.read()
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="screenshot.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode() + img_data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning("Telegram 사진 전송 실패: %s", e)


def _notify(text: str) -> None:
    """Telegram으로 상태 알림 (선택적 — 환경변수 없으면 로그만)."""
    try:
        import urllib.request, json as _json
        token = os.environ.get("LOG_TELEGRAM_BOT_TOKEN") or CFG.get("telegram", {}).get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("LOG_TELEGRAM_CHAT_ID") or CFG.get("telegram", {}).get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        payload = _json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.warning("Telegram 알림 실패: %s", e)


def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=WORK_DIR, capture_output=True, text=True, check=check)


def _branch_exists(branch: str) -> bool:
    r = _git(["branch", "--list", branch], check=False)
    return branch in r.stdout


def _notify_diff(task_id: str, branch: str) -> None:
    """완료된 태스크의 변경 파일 목록 + 통계를 Telegram으로 전송."""
    try:
        # 변경된 파일 목록 (master 대비)
        stat = _git(["diff", "master..HEAD", "--stat"], check=False)
        files = _git(["diff", "master..HEAD", "--name-status"], check=False)

        if not files.stdout.strip():
            return

        lines = []
        added, modified, deleted = [], [], []
        for line in files.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            status, path = parts[0][0], parts[1]
            if status == "A":
                added.append(path)
            elif status == "M":
                modified.append(path)
            elif status == "D":
                deleted.append(path)

        if added:
            lines.append(f"➕ <b>추가</b> ({len(added)}개)\n" + "\n".join(f"  <code>{f}</code>" for f in added))
        if modified:
            lines.append(f"✏️ <b>수정</b> ({len(modified)}개)\n" + "\n".join(f"  <code>{f}</code>" for f in modified))
        if deleted:
            lines.append(f"🗑 <b>삭제</b> ({len(deleted)}개)\n" + "\n".join(f"  <code>{f}</code>" for f in deleted))

        # --stat 마지막 줄 (예: "3 files changed, 45 insertions(+), 2 deletions(-)")
        summary = stat.stdout.strip().splitlines()[-1] if stat.stdout.strip() else ""

        msg = (
            f"📋 <b>변경 내역</b> <code>{task_id}</code>\n"
            + "\n\n".join(lines)
            + (f"\n\n📊 {summary}" if summary else "")
        )
        _notify(msg)
    except Exception as e:
        log.warning("diff 알림 실패: %s", e)


def _step(task_id: str, step: str, msg: str) -> None:
    """태스크 진행 단계 기록 + Telegram 알림."""
    update_task(task_id, result=f"[{step}] {msg}")
    _notify(msg)
    log.info("태스크 %s [%s]: %s", task_id, step, msg)


def delete_branch(branch: str) -> None:
    """로컬 + 원격 브랜치 삭제 (없으면 무시)."""
    _git(["branch", "-D", branch], check=False)
    _git(["push", "origin", "--delete", branch], check=False)


def _create_pr(repo, task_id: str, branch: str, description: str, plan: dict, review: dict = None) -> str:
    """GitHub PR 생성 후 PR URL 반환. 실패 시 빈 문자열."""
    try:
        approach = plan.get("approach", "")
        files = plan.get("files", [])
        body_lines = [f"## 태스크 `{task_id}`\n"]
        if approach:
            body_lines.append(f"### 구현 방향\n{approach}\n")
        if files:
            body_lines.append("### 관련 파일\n" + "\n".join(f"- `{f}`" for f in files))
        if review and review.get("summary"):
            score = review.get("score", 0)
            body_lines.append(f"### AI 코드 리뷰 ({score}/10)\n{review['summary']}")
            if review.get("issues"):
                body_lines.append("#### 지적 사항\n" + "\n".join(f"- {i}" for i in review["issues"]))
        body_lines.append("\n---\n🤖 AI 자동 생성 — `/merge {id}` 로 승인, `/reject {id}` 로 거부".replace("{id}", task_id))
        pr = repo.create_pull(
            title=f"[AI] {description[:72]}",
            body="\n".join(body_lines),
            head=branch,
            base="master",
        )
        log.info("PR 생성: %s", pr.html_url)
        return pr.html_url
    except Exception as e:
        log.warning("PR 생성 실패: %s", e)
        return ""


def process_pending_task(model, repo=None) -> bool:
    """대기 중인 태스크 1개를 처리. 처리했으면 True 반환."""
    task = pop_next_pending()
    if not task:
        return False

    task_id = task["id"]
    branch = task["branch"]
    description = task["description"]
    retries = task.get("retries", 0)

    _step(task_id, "start",
          f"🔄 <b>태스크 시작</b> (시도 {retries + 1}/{MAX_RETRIES})\n"
          f"<code>{task_id}</code>: {description[:80]}")

    try:
        # ── 0. 태스크 분해 (Phase 4) — child 태스크는 분해 안 함 ────────
        is_child = task.get("source", "").startswith("decomposed:")
        if not is_child and retries == 0:
            children = _decompose_task(task_id, description, model)
            if children:
                return True  # 분해 완료 — 서브태스크가 큐에 등록됨

        # ── 1. 브랜치 생성 ──────────────────────────────────────────────
        _git(["checkout", "master"])
        _git(["pull", "--ff-only"], check=False)
        if _branch_exists(branch):
            _git(["branch", "-D", branch])
        _git(["checkout", "-b", branch])
        _step(task_id, "branch", f"🌿 브랜치 생성 완료: <code>{branch}</code>")

        # ── 2. AI 패치 생성 (Phase 2: plan→execute 2단계) ───────────────
        # Phase 2 Step 1+2: 계획 수립 → import 그래프 기반 컨텍스트 선택
        _step(task_id, "planning", f"🗺 계획 수립 중...\n<code>{task_id}</code>")
        try:
            plan = model.plan_changes(description)
            approach = plan.get("approach", "")[:80]
            plan_files = plan.get("files", [])
            _step(task_id, "planned",
                  f"📋 계획: {approach}\n파일: {', '.join(plan_files[:3])}"
                  + (f" 외 {len(plan_files)-3}개" if len(plan_files) > 3 else ""))
            code_context = select_context(plan, WORK_DIR, _is_protected)
        except Exception as e:
            log.warning("계획 수립 실패, 전체 스캔으로 폴백: %s", e)
            code_context = get_code_context()

        _step(task_id, "generating", f"🤖 AI 코드 생성 중...\n<code>{task_id}</code>")
        patch = model.generate_patch(description, code_context)

        if not patch.strip():
            update_task(task_id, status="failed", result="AI가 패치를 반환하지 않았습니다.")
            _git(["checkout", "master"], check=False)
            delete_branch(branch)
            _notify(f"❌ <b>태스크 실패</b> — AI 패치 없음\n<code>{task_id}</code>")
            return True

        # ── 3. 패치 적용 ────────────────────────────────────────────────
        _step(task_id, "applying", f"🔧 패치 적용 중...\n<code>{task_id}</code>")
        if not apply_patch(patch):
            _step(task_id, "fallback", f"⚠️ 패치 실패 — 전체 파일 재생성 시도...\n<code>{task_id}</code>")
            try:
                # 패치 실패 시 전체 파일 생성 방식으로 폴백
                files = model.generate_code(description)
                if not files:
                    raise ValueError("AI가 생성된 파일을 반환하지 않았습니다.")
                
                for file_path, content in files.items():
                    p = WORK_DIR / file_path
                    if _is_protected(p):
                        log.warning("보호 파일 쓰기 시도 차단 (폴백): %s", file_path)
                        continue
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
                    _git(["add", str(file_path)])
                _step(task_id, "fallback_applied", "✅ 전체 파일 쓰기 완료")
            except Exception as e:
                if retries < MAX_RETRIES - 1:
                    update_task(task_id, status="pending", retries=retries + 1)
                    _git(["checkout", "master"], check=False)
                    delete_branch(branch)
                    _notify(
                        f"⚠️ <b>패치 & 폴백 실패 — 재시도 예약</b> ({retries + 1}/{MAX_RETRIES})\n"
                        f"<code>{task_id}</code>\n{str(e)[:100]}"
                    )
                else:
                    update_task(task_id, status="failed", result=f"패치/폴백 {MAX_RETRIES}회 실패: {e}")
                    _git(["checkout", "master"], check=False)
                    delete_branch(branch)
                    _notify(f"❌ <b>태스크 최대 재시도 초과</b>\n<code>{task_id}</code>")
                return True

        # ── 4. 자동 테스트 검수 (Phase 2 Step 5) ────────────────────────
        _step(task_id, "testing", f"🧪 테스트 실행 중...\n<code>{task_id}</code>")
        passed, screenshot, test_output = _run_tests(task_id)
        if not passed:
            caption = f"❌ 테스트 실패 [{task_id}]\n{test_output[-200:]}"
            if screenshot:
                _notify_photo(screenshot, caption)
            else:
                _notify(f"❌ <b>테스트 실패</b>\n<code>{task_id}</code>\n<pre>{test_output[-300:]}</pre>")

            if retries < MAX_RETRIES - 1:
                update_task(task_id, status="pending", retries=retries + 1)
                _git(["checkout", "master"], check=False)
                delete_branch(branch)
                _notify(
                    f"⚠️ <b>테스트 실패 — 재시도 예약</b> ({retries + 1}/{MAX_RETRIES})\n"
                    f"<code>{task_id}</code>"
                )
            else:
                update_task(task_id, status="failed",
                            result=f"테스트 {MAX_RETRIES}회 실패: {test_output[-100:]}")
                _git(["checkout", "master"], check=False)
                delete_branch(branch)
                _notify(f"❌ <b>테스트 최대 재시도 초과</b>\n<code>{task_id}</code>")
            return True

        _step(task_id, "test_pass", f"✅ 테스트 통과\n<code>{task_id}</code>")

        # ── 5. AI 코드 리뷰 (Phase 3) ────────────────────────────────────
        _step(task_id, "reviewing", f"🔍 코드 리뷰 중...\n<code>{task_id}</code>")
        review = _review_changes(task_id, description, model)
        if review.get("one_line"):
            score = review.get("score", 0)
            score_emoji = "🟢" if score >= 8 else "🟡" if score >= 5 else "🔴"
            _notify(
                f"🔍 <b>코드 리뷰 완료</b>\n"
                f"<code>{task_id}</code>\n"
                f"📦 {review['one_line']}\n"
                f"{score_emoji} 점수: {score}/10"
                + (f"\n⚠️ " + "\n⚠️ ".join(review['issues']) if review.get('issues') else "")
            )

        # ── 6. 커밋 & Push (Phase 2 Step 4: 논리 단위별 분할 커밋) ────────
        _step(task_id, "pushing",
              f"⬆️ 커밋 & Push 중...\n<code>{branch}</code>")
        ok, err = commit_in_groups(task_id, description, branch)
        if ok:
            # ── 5. PR 생성 (Phase 2 Step 3) ──────────────────────────────
            pr_url = ""
            if repo is not None:
                pr_url = _create_pr(repo, task_id, branch, description,
                                    plan if "plan" in dir() else {},
                                    review if "review" in dir() else {})
                if pr_url:
                    update_task(task_id, status="completed",
                                result=f"브랜치 {branch} push 완료", pr_url=pr_url)
                    _notify(
                        f"✅ <b>태스크 완료 — PR 생성됨</b>\n"
                        f"<code>{task_id}</code>: {description[:60]}\n"
                        f"🌿 <code>{branch}</code>\n"
                        f"🔗 {pr_url}\n\n"
                        f"👉 <code>/merge {task_id}</code> 로 승인"
                    )
                else:
                    update_task(task_id, status="completed",
                                result=f"브랜치 {branch} push 완료 (PR 생성 실패)")
                    _notify(
                        f"✅ <b>태스크 완료</b> (PR 생성 실패)\n"
                        f"<code>{task_id}</code>: {description[:60]}\n"
                        f"🌿 <code>{branch}</code>"
                    )
            else:
                update_task(task_id, status="completed", result=f"브랜치 {branch} push 완료")
                _notify(
                    f"✅ <b>태스크 완료</b>\n"
                    f"<code>{task_id}</code>: {description[:60]}\n"
                    f"🌿 <code>{branch}</code>"
                )
            _notify_diff(task_id, branch)
        else:
            update_task(task_id, status="failed", result=err)
            _notify(f"❌ <b>Push 실패</b>\n<code>{task_id}</code>\n<code>{err}</code>")

        # 서브태스크 완료 시 부모 상태 확인
        if is_child:
            parent_id = task.get("parent_id", "")
            if parent_id:
                from core.task_queue import get_task, list_tasks as _lt
                siblings = [t for t in _lt("completed") if t.get("parent_id") == parent_id]
                parent = get_task(parent_id)
                if parent:
                    total_children = len(parent.get("result", "").split(",")) if parent.get("result") else 0
                    if len(siblings) >= total_children:
                        update_task(parent_id, status="completed",
                                    result=f"서브태스크 {len(siblings)}개 모두 완료")
                        _notify(f"✅ <b>전체 태스크 완료</b>\n<code>{parent_id}</code> — 서브태스크 {len(siblings)}개 완료")

        _git(["checkout", "master"], check=False)

    except Exception as e:
        log.error("태스크 처리 오류 %s: %s", task_id, e)
        update_task(task_id, status="failed", result=str(e))
        _notify(f"❌ <b>태스크 오류</b>\n<code>{task_id}</code>: {e}")
        _git(["checkout", "master"], check=False)

    return True


def get_failed_run(repo):
    """가장 최근 실패한 workflow run을 반환."""
    for run in repo.get_workflow_runs(status="failure", branch="master"):
        return run
    return None


def get_run_log(run) -> str:
    """실패한 run의 로그를 텍스트로 반환."""
    try:
        import urllib.request
        import zipfile
        import io
        log_url = run.logs_url
        headers = {"Authorization": f"token {CFG['github']['token']}",
                   "Accept": "application/vnd.github+json"}
        req = urllib.request.Request(log_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            zip_data = resp.read()
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            texts = []
            for name in zf.namelist():
                texts.append(zf.read(name).decode(errors="replace"))
            return "\n".join(texts)[-8000:]  # 마지막 8000자
    except Exception as e:
        log.warning("로그 다운로드 실패: %s", e)
        return ""


def get_code_context() -> str:
    """수정 대상 코드 파일들을 하나의 문자열로 합침. 보호 파일 제외."""
    extensions = {".py", ".js", ".ts", ".jsx", ".tsx"}
    parts = []
    for ext in extensions:
        for p in sorted(WORK_DIR.rglob(f"*{ext}")):
            if _is_protected(p):
                continue
            try:
                content = p.read_text(errors="replace")
                parts.append(f"### {p.relative_to(WORK_DIR)}\n{content}")
            except Exception:
                pass
    return "\n\n".join(parts)[:12000]


def apply_patch(patch: str) -> bool:
    """AI가 반환한 unified diff 패치를 적용."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
        f.write(patch)
        patch_file = f.name
    try:
        result = subprocess.run(
            ["git", "apply", "--whitespace=fix", patch_file],
            cwd=WORK_DIR, capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning("패치 적용 실패:\n%s", result.stderr)
            return False
        # 보호 파일 수정 시 패치 되돌림
        touched = subprocess.run(
            ["git", "diff", "--name-only"], cwd=WORK_DIR, capture_output=True, text=True
        ).stdout.splitlines()
        blocked = [f for f in touched if _is_protected(WORK_DIR / f)]
        if blocked:
            subprocess.run(["git", "checkout", "--"] + touched, cwd=WORK_DIR, capture_output=True)
            log.warning("보호 파일 수정 시도 차단: %s", blocked)
            return False
        return True
    finally:
        os.unlink(patch_file)


def commit_and_push(message: str, branch: str = None) -> tuple[bool, str]:
    """변경사항 커밋 후 push. (success, error_msg) 반환."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=WORK_DIR, check=True, capture_output=True)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=WORK_DIR, capture_output=True, text=True)
        if not status.stdout.strip():
            return False, "변경사항 없음 — AI가 코드를 수정하지 않았습니다."
        r = subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR, capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" in r.stdout + r.stderr:
            return False, "변경사항 없음 — AI가 코드를 수정하지 않았습니다."
        if r.returncode != 0:
            return False, f"commit 실패: {r.stderr.strip()[:200]}"
        push_args = ["git", "push", "-u", "origin", branch] if branch else ["git", "push"]
        r = subprocess.run(push_args, cwd=WORK_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"push 실패: {r.stderr.strip()[:200]}"
        return True, ""
    except subprocess.CalledProcessError as e:
        err = (e.stderr or str(e))[:200]
        log.error("commit/push 실패: %s", err)
        return False, err


def _commit_type(rel_path: str) -> tuple[str, str]:
    """파일 경로 → (커밋 타입, 스코프) 추론.

    예) "agent/handlers/bot_commands.py" → ("feat", "handlers")
        "tests/test_api.py"             → ("test", "tests")
        "README.md"                     → ("docs", "docs")
    """
    p = Path(rel_path)
    parts = p.parts
    stem = p.stem.lower()

    if not parts:
        return "chore", "misc"
    if "test" in stem or parts[0] == "tests":
        return "test", "tests"
    if p.suffix in (".md", ".rst", ".txt"):
        return "docs", "docs"
    if p.name in ("config.yaml", "pyproject.toml", "requirements.txt",
                  "setup.py", "setup.cfg", ".env.example"):
        return "config", p.stem
    if parts[0] == "agent":
        scope = parts[1] if len(parts) > 2 else "agent"
        return "feat", scope
    return "feat", parts[0]


def commit_in_groups(task_id: str, description: str, branch: str) -> tuple[bool, str]:
    """변경 파일을 논리 단위로 분할해 여러 커밋 생성 후 push.

    흐름:
    1. 전체 스테이징 해제 (unstage) → 워킹트리 기준으로 파일 목록 확보
    2. 파일 경로별 (타입, 스코프) 그룹핑
    3. 그룹별 git add + commit
    4. 마지막에 한 번 push

    단일 그룹이면 commit_and_push()와 동일 결과.
    """
    # 이미 스테이지된 파일도 unstage 해서 porcelain로 전체 파악
    subprocess.run(["git", "reset", "HEAD"], cwd=WORK_DIR, capture_output=True)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=WORK_DIR, capture_output=True, text=True
    )
    if not status.stdout.strip():
        return False, "변경사항 없음 — AI가 코드를 수정하지 않았습니다."

    # porcelain 출력: "XY path" 또는 "XY old -> new"
    changed: list[str] = []
    for line in status.stdout.splitlines():
        raw = line[3:].strip()
        path = raw.split(" -> ")[-1]  # rename 대응
        if path:
            changed.append(path)

    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for f in changed:
        groups[_commit_type(f)].append(f)

    short = description[:50]

    # 그룹 1개면 단일 커밋
    if len(groups) == 1:
        (type_, scope), files = next(iter(groups.items()))
        subprocess.run(["git", "add"] + files, cwd=WORK_DIR, capture_output=True)
        r = subprocess.run(
            ["git", "commit", "-m", f"{type_}({scope}): [{task_id}] {short}"],
            cwd=WORK_DIR, capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, f"commit 실패: {r.stderr.strip()[:200]}"
    else:
        # 여러 그룹: 각각 커밋
        for (type_, scope), files in groups.items():
            subprocess.run(["git", "add"] + files, cwd=WORK_DIR, capture_output=True)
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=WORK_DIR, capture_output=True, text=True
            )
            if not staged.stdout.strip():
                continue
            r = subprocess.run(
                ["git", "commit", "-m", f"{type_}({scope}): [{task_id}] {short}"],
                cwd=WORK_DIR, capture_output=True, text=True,
            )
            if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
                return False, f"commit 실패 ({scope}): {r.stderr.strip()[:150]}"

        log.info("커밋 분할 완료: %d그룹", len(groups))

    # push
    r = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=WORK_DIR, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, f"push 실패: {r.stderr.strip()[:200]}"
    return True, ""


def _decompose_task(task_id: str, description: str, model) -> list[str]:
    """태스크가 복잡하면 서브태스크로 분해 후 큐에 등록. 서브태스크 ID 목록 반환.

    부모 태스크가 있는 경우(child) 분해 안 함 — 무한 분해 방지.
    """
    try:
        result = model.decompose_task(description[:800])
        log.info("태스크 분해 판단: is_complex=%s, subtasks=%d개",
                 result.get("is_complex"), len(result.get("subtasks", [])))
        if not result.get("is_complex"):
            log.info("태스크 %s — 단순 태스크로 판단, 분해 없이 진행", task_id)
            return []
        subtasks_desc = result.get("subtasks", [])
        if len(subtasks_desc) < 2:
            return []

        child_ids: list[str] = []
        for desc in subtasks_desc:
            child = create_task(desc, source=f"decomposed:{task_id}")
            update_task(child["id"], parent_id=task_id)
            child_ids.append(child["id"])

        update_task(task_id, status="decomposed",
                    result=f"서브태스크 {len(child_ids)}개로 분해: {', '.join(child_ids)}")
        _notify(
            f"🔀 <b>태스크 분해됨</b>\n"
            f"<code>{task_id}</code> → {len(child_ids)}개 서브태스크\n"
            + "\n".join(f"  • <code>{cid}</code>: {subtasks_desc[i][:60]}"
                        for i, cid in enumerate(child_ids))
        )
        log.info("태스크 %s → 서브태스크 %d개 생성", task_id, len(child_ids))
        return child_ids
    except Exception as e:
        log.warning("태스크 분해 실패 (단일 실행으로 진행): %s", e)
        return []


def _review_changes(task_id: str, description: str, model) -> dict:
    """AI 코드 리뷰 — 현재 워킹트리 diff를 분석해 요약·점수·이슈 반환."""
    try:
        diff = subprocess.run(
            ["git", "diff", "master..HEAD", "--unified=3"],
            cwd=WORK_DIR, capture_output=True, text=True
        ).stdout
        if not diff.strip():
            # staged but not committed yet — diff against HEAD
            diff = subprocess.run(
                ["git", "diff"],
                cwd=WORK_DIR, capture_output=True, text=True
            ).stdout
        diff = diff[:6000]  # 토큰 절약
        review = model.review_code(description[:500], diff)
        update_task(task_id, review=review)
        return review
    except Exception as e:
        log.warning("코드 리뷰 실패: %s", e)
        return {"one_line": "", "summary": "", "score": 0, "issues": []}


def _run_tests(task_id: str) -> tuple[bool, str, str]:
    """tests/runner.py 실행 후 (통과여부, 스크린샷경로, 출력) 반환.

    HEADLESS=1 환경변수로 headless 모드 강제.
    tests/runner.py 없으면 스킵(통과로 처리).
    """
    runner = WORK_DIR / "tests" / "runner.py"
    if not runner.exists():
        log.info("tests/runner.py 없음 — 테스트 스킵")
        return True, "", "테스트 파일 없음 (스킵)"

    python = Path(sys.executable)
    env = os.environ.copy()
    env["HEADLESS"] = "1"
    env["PYTHONPATH"] = str(WORK_DIR)

    try:
        r = subprocess.run(
            [str(python), str(runner)],
            cwd=WORK_DIR,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        output = (r.stdout + r.stderr).strip()[-1000:]
        passed = r.returncode == 0

        # 실패 스크린샷 경로 추출 ([SCREENSHOT] /tmp/... 형식)
        screenshot = ""
        for line in (r.stdout + r.stderr).splitlines():
            if line.startswith("[SCREENSHOT]"):
                screenshot = line.split("]", 1)[-1].strip()

        return passed, screenshot, output
    except subprocess.TimeoutExpired:
        return False, "", "테스트 타임아웃 (120s 초과)"
    except Exception as e:
        return False, "", f"테스트 실행 오류: {e}"


def cancel_run(run) -> None:
    try:
        run.cancel()
    except Exception:
        pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    token = CFG["github"]["token"] or os.environ.get("GITHUB_TOKEN")
    if not token:
        log.error("GitHub API 토큰이 없습니다. config.yaml 또는 .env를 확인해 주세요.")
        sys.exit(1)

    gh = Github(token)
    repo = gh.get_repo(CFG["github"]["repo"])
    model = get_model(CFG)

    log.info("AI 피드백 루프 시작 (폴링 %ds, 최대 %d회 재시도)", POLL_INTERVAL, MAX_RETRIES)
    seen_runs: set[int] = set()
    retry_count: dict[int, int] = {}

    while True:
        try:
            # ── 우선순위 1: CI 실패 자동 수정 ────────────────────────────
            try:
                run = None # get_failed_run(repo)
                if run and run.id not in seen_runs:
                    retries = retry_count.get(run.id, 0)
                    if retries >= MAX_RETRIES:
                        log.warning("Run #%s 최대 재시도 초과. 건너뜀.", run.id)
                        seen_runs.add(run.id)
                    else:
                        log.info("실패 감지: Run #%s (재시도 %d/%d)", run.id, retries + 1, MAX_RETRIES)
                        error_log = get_run_log(run)
                        code_context = get_code_context()
                        patch = model.fix_code(error_log, code_context)

                        if not patch.strip():
                            log.warning("AI가 패치를 반환하지 않았습니다.")
                            seen_runs.add(run.id)
                        elif apply_patch(patch):
                            msg = f"fix: AI 자동 수정 (run #{run.id}, 시도 {retries + 1})"
                            ok, err = commit_and_push(msg)
                            if ok:
                                log.info("수정 push 완료.")
                                retry_count[run.id] = retries + 1
                                seen_runs.add(run.id)
                            else:
                                log.error("push 실패: %s", err)
                        else:
                            log.warning("패치 적용 실패.")
                            seen_runs.add(run.id)
            except Exception as e:
                log.error("CI 감시 루프 오류 (Priority 1): %s", e)

            # ── 우선순위 2: 채팅 태스크 큐 처리 ─────────────────────────
            try:
                process_pending_task(model, repo)
            except Exception as e:
                log.error("태스크 큐 처리 오류 (Priority 2): %s", e)

        except GithubException as e:
            log.error("GitHub API 전체 오류: %s", e)
        except KeyboardInterrupt:
            log.info("루프 종료")
            break
        except Exception as e:
            log.error("메인 루프 예기치 않은 오류: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
