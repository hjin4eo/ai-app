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
from pathlib import Path

import yaml
from github import Github, GithubException

_agent_dir = Path(__file__).parent.resolve()
if str(_agent_dir) not in sys.path:
    sys.path.append(str(_agent_dir))

from core.bot_config import CFG
from core.models import get_model
from core.task_queue import pop_next_pending, update_task, list_tasks

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


def process_pending_task(model) -> bool:
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
        # ── 1. 브랜치 생성 ──────────────────────────────────────────────
        _git(["checkout", "master"])
        _git(["pull", "--ff-only"], check=False)
        if _branch_exists(branch):
            _git(["branch", "-D", branch])
        _git(["checkout", "-b", branch])
        _step(task_id, "branch", f"🌿 브랜치 생성 완료: <code>{branch}</code>")

        # ── 2. AI 패치 생성 ─────────────────────────────────────────────
        _step(task_id, "generating", f"🤖 AI 코드 생성 중...\n<code>{task_id}</code>")
        code_context = get_code_context()
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

        # ── 4. 커밋 & Push ───────────────────────────────────────────────
        _step(task_id, "pushing",
              f"⬆️ 커밋 & Push 중...\n<code>{branch}</code>")
        msg = f"feat: AI 태스크 {task_id} — {description[:60]}"
        ok, err = commit_and_push(msg, branch=branch)
        if ok:
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
                process_pending_task(model)
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
