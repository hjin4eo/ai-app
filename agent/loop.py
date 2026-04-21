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

log = logging.getLogger(__name__)


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
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
                    # 수정된 파일만 정밀하게 Git 인덱스에 추가
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
        if commit_and_push(msg, branch=branch):
            update_task(task_id, status="completed", result=f"브랜치 {branch} push 완료")
            _notify(
                f"✅ <b>태스크 완료</b>\n"
                f"<code>{task_id}</code>: {description[:60]}\n"
                f"🌿 <code>{branch}</code>"
            )
        else:
            update_task(task_id, status="failed", result="push 실패")
            _notify(f"❌ <b>Push 실패</b>\n<code>{task_id}</code>")

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
    """수정 대상 코드 파일들을 하나의 문자열로 합침."""
    extensions = {".py", ".js", ".ts", ".jsx", ".tsx"}
    parts = []
    for ext in extensions:
        for p in sorted(WORK_DIR.rglob(f"*{ext}")):
            # venv, node_modules, .git 제외
            if any(skip in p.parts for skip in ("venv", ".venv", "node_modules", ".git")):
                continue
            try:
                content = p.read_text(errors="replace")
                parts.append(f"### {p.relative_to(WORK_DIR)}\n{content}")
            except Exception:
                pass
    return "\n\n".join(parts)[:12000]  # 12000자 제한


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
        return True
    finally:
        os.unlink(patch_file)


def commit_and_push(message: str, branch: str = None) -> bool:
    """변경사항 커밋 후 push. branch 지정 시 원격에 새 브랜치로 push."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=WORK_DIR, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR, check=True)
        if branch:
            subprocess.run(["git", "push", "-u", "origin", branch], cwd=WORK_DIR, check=True)
        else:
            subprocess.run(["git", "push"], cwd=WORK_DIR, check=True)
        return True
    except subprocess.CalledProcessError as e:
        log.error("commit/push 실패: %s", e)
        return False


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
                            if commit_and_push(msg):
                                log.info("수정 push 완료.")
                                retry_count[run.id] = retries + 1
                                seen_runs.add(run.id)
                            else:
                                log.error("push 실패")
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
