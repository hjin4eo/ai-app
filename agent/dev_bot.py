#!/usr/bin/env python3
"""
dev_bot.py — 개발해봇
개발 워크플로우 전용 Telegram 봇 (LOG_TELEGRAM_BOT_TOKEN 사용)
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

_agent_dir = Path(__file__).parent.resolve()
if str(_agent_dir) not in sys.path:
    sys.path.append(str(_agent_dir))

from core.bot_config import CFG
from core.task_queue import create_task, cancel_task, get_task, list_tasks, STATES

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("LOG_TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("LOG_TELEGRAM_CHAT_ID", "0"))
WORK_DIR = Path(CFG["agent"].get("work_dir", ".")).resolve()
LOG_FILE = Path("/home/home/loop.log")


def _admin(update: Update) -> bool:
    return update.effective_chat.id == CHAT_ID


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=WORK_DIR, capture_output=True, text=True)


# ── /help ─────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🛠 <b>개발해봇 명령어</b>\n\n"
        "📋 <b>AI 태스크</b>\n"
        "  /task &lt;작업 설명&gt; — AI에게 개발 작업 요청\n"
        "  /task status — 전체 태스크 현황\n"
        "  /task status &lt;id&gt; — 특정 태스크 상세\n"
        "  /task cancel &lt;id&gt; — 태스크 취소\n\n"
        "🔀 <b>브랜치 관리</b>\n"
        "  /merge &lt;id&gt; — 태스크 브랜치를 master에 머지\n"
        "  /reject &lt;id&gt; — 태스크 브랜치 삭제 (거부)\n"
        "  /diff &lt;id&gt; — 태스크 변경사항 보기\n\n"
        "📊 <b>모니터링</b>\n"
        "  /ci_status — 최근 CI 실행 결과\n"
        "  /loop_status — AI 루프 실행 상태\n"
        "  /log [n] — loop.log 최근 n줄 (기본 30)\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ── /task ─────────────────────────────────────────────────────────────────────

async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    args = context.args or []

    # /task status [id]
    if args and args[0] == "status":
        if len(args) > 1:
            task = get_task(args[1])
            if not task:
                await update.message.reply_text(f"태스크 <code>{args[1]}</code> 없음", parse_mode="HTML")
                return
            await update.message.reply_text(
                f"🆔 <code>{task['id']}</code>\n"
                f"📌 상태: <b>{task['status']}</b>\n"
                f"📝 {task['description'][:100]}\n"
                f"🌿 <code>{task.get('branch', '-')}</code>\n"
                f"🔁 재시도: {task.get('retries', 0)}\n"
                f"📄 {task.get('result', '')[:200]}",
                parse_mode="HTML"
            )
        else:
            icons = {"pending": "⏳", "in_progress": "🔄", "completed": "✅", "cancelled": "🚫", "failed": "❌"}
            lines = []
            total = 0
            for state in STATES:
                tasks = list_tasks(state)
                if not tasks:
                    continue
                total += len(tasks)
                lines.append(f"{icons[state]} <b>{state.upper()}</b> ({len(tasks)}개)")
                for t in tasks[:3]:
                    lines.append(f"  <code>{t['id']}</code>: {t['description'][:40]}")
                if len(tasks) > 3:
                    lines.append(f"  ...외 {len(tasks)-3}개")
            if not total:
                await update.message.reply_text("태스크 없음")
                return
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    # /task cancel <id>
    if args and args[0] == "cancel":
        if len(args) < 2:
            await update.message.reply_text("사용법: /task cancel &lt;id&gt;", parse_mode="HTML")
            return
        task = cancel_task(args[1])
        if not task:
            await update.message.reply_text(f"태스크 <code>{args[1]}</code> 없음", parse_mode="HTML")
            return
        # 브랜치 삭제
        branch = task.get("branch", "")
        if branch:
            _git(["branch", "-D", branch])
            _git(["push", "origin", "--delete", branch])
        await update.message.reply_text(
            f"🚫 <b>취소됨</b>\n<code>{task['id']}</code>: {task['description'][:60]}",
            parse_mode="HTML"
        )
        return

    # /task <description>
    if not args:
        await update.message.reply_text(
            "사용법:\n"
            "  /task &lt;작업 설명&gt;\n"
            "  /task status\n"
            "  /task cancel &lt;id&gt;",
            parse_mode="HTML"
        )
        return

    description = " ".join(args)
    task = create_task(description, source="dev_bot")
    await update.message.reply_text(
        f"✅ <b>태스크 등록됨</b>\n\n"
        f"🆔 <code>{task['id']}</code>\n"
        f"🌿 브랜치: <code>{task['branch']}</code>\n"
        f"📝 {description[:100]}",
        parse_mode="HTML"
    )


# ── /merge ────────────────────────────────────────────────────────────────────

async def cmd_merge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    if not context.args:
        await update.message.reply_text("사용법: /merge &lt;task-id&gt;", parse_mode="HTML")
        return

    task_id = context.args[0]
    task = get_task(task_id)
    branch = f"ai/task-{task_id}" if not task else task.get("branch", f"ai/task-{task_id}")

    msg = await update.message.reply_text(f"🔀 <code>{branch}</code> → master 머지 중...", parse_mode="HTML")

    def do_merge():
        _git(["checkout", "master"])
        _git(["pull", "--ff-only"])
        r = _git(["merge", "--no-ff", branch, "-m", f"merge: 태스크 {task_id} 승인 머지"])
        if r.returncode != 0:
            return False, r.stderr.strip()[:300]
        r = _git(["push", "origin", "master"])
        if r.returncode != 0:
            return False, r.stderr.strip()[:300]
        _git(["branch", "-d", branch])
        _git(["push", "origin", "--delete", branch])
        return True, ""

    loop = asyncio.get_event_loop()
    ok, err = await loop.run_in_executor(None, do_merge)

    if ok:
        await msg.edit_text(f"✅ <b>머지 완료</b>\n<code>{branch}</code> → master", parse_mode="HTML")
    else:
        await msg.edit_text(f"❌ <b>머지 실패</b>\n<code>{err}</code>", parse_mode="HTML")


# ── /reject ───────────────────────────────────────────────────────────────────

async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    if not context.args:
        await update.message.reply_text("사용법: /reject &lt;task-id&gt;", parse_mode="HTML")
        return

    task_id = context.args[0]
    branch = f"ai/task-{task_id}"

    _git(["branch", "-D", branch])
    _git(["push", "origin", "--delete", branch])

    task = get_task(task_id)
    if task:
        from core.task_queue import update_task
        update_task(task_id, status="cancelled", result="사용자 거부")

    await update.message.reply_text(
        f"🗑 <b>브랜치 삭제됨</b>\n<code>{branch}</code>",
        parse_mode="HTML"
    )


# ── /diff ─────────────────────────────────────────────────────────────────────

async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    if not context.args:
        await update.message.reply_text("사용법: /diff &lt;task-id&gt;", parse_mode="HTML")
        return

    task_id = context.args[0]
    branch = f"ai/task-{task_id}"

    def get_diff():
        stat = _git(["diff", f"master..{branch}", "--stat"])
        files = _git(["diff", f"master..{branch}", "--name-status"])
        return stat.stdout.strip(), files.stdout.strip()

    loop = asyncio.get_event_loop()
    stat, files = await loop.run_in_executor(None, get_diff)

    if not files:
        await update.message.reply_text(f"브랜치 <code>{branch}</code> 없거나 변경사항 없음", parse_mode="HTML")
        return

    added, modified, deleted = [], [], []
    for line in files.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        s, p = parts[0][0], parts[1]
        if s == "A": added.append(p)
        elif s == "M": modified.append(p)
        elif s == "D": deleted.append(p)

    lines = [f"📋 <b>변경 내역</b> <code>{task_id}</code>\n"]
    if added:
        lines.append("➕ <b>추가</b>\n" + "\n".join(f"  <code>{f}</code>" for f in added))
    if modified:
        lines.append("✏️ <b>수정</b>\n" + "\n".join(f"  <code>{f}</code>" for f in modified))
    if deleted:
        lines.append("🗑 <b>삭제</b>\n" + "\n".join(f"  <code>{f}</code>" for f in deleted))

    summary = stat.splitlines()[-1] if stat else ""
    if summary:
        lines.append(f"\n📊 {summary}")

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


# ── /ci_status ────────────────────────────────────────────────────────────────

async def cmd_ci_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return

    def check():
        try:
            from github import Github
            token = os.environ.get("GITHUB_TOKEN") or CFG.get("github", {}).get("token", "")
            repo_name = CFG.get("github", {}).get("repo", "")
            gh = Github(token)
            repo = gh.get_repo(repo_name)
            runs = list(repo.get_workflow_runs(branch="master"))[:5]
            if not runs:
                return "최근 CI 실행 없음"
            lines = ["📊 <b>최근 CI (5개)</b>\n"]
            for r in runs:
                icon = "✅" if r.conclusion == "success" else "❌" if r.conclusion == "failure" else "⏳"
                lines.append(f"{icon} #{r.id} — {r.conclusion or r.status}  {r.created_at.strftime('%m-%d %H:%M')}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ {e}"

    result = await asyncio.get_event_loop().run_in_executor(None, check)
    await update.message.reply_text(result, parse_mode="HTML")


# ── /loop_status ──────────────────────────────────────────────────────────────

async def cmd_loop_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    r = subprocess.run(["pgrep", "-af", "loop.py"], capture_output=True, text=True)
    procs = [l for l in r.stdout.splitlines() if "loop.py" in l and "pgrep" not in l]
    if procs:
        await update.message.reply_text("✅ <b>AI 루프 실행 중</b>\n\n" + "\n".join(procs), parse_mode="HTML")
    else:
        await update.message.reply_text("⚠️ AI 루프가 실행되고 있지 않습니다.")


# ── /log ──────────────────────────────────────────────────────────────────────

async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin(update):
        return
    n = int(context.args[0]) if context.args and context.args[0].isdigit() else 30
    n = min(n, 100)

    if not LOG_FILE.exists():
        await update.message.reply_text("loop.log 없음")
        return

    lines = LOG_FILE.read_text(errors="replace").splitlines()[-n:]
    text = "\n".join(lines)[-3500:]
    await update.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if not BOT_TOKEN:
        log.error("LOG_TELEGRAM_BOT_TOKEN 없음")
        sys.exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("merge", cmd_merge))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("diff", cmd_diff))
    app.add_handler(CommandHandler("ci_status", cmd_ci_status))
    app.add_handler(CommandHandler("loop_status", cmd_loop_status))
    app.add_handler(CommandHandler("log", cmd_log))

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("help", "명령어 안내"),
            BotCommand("task", "AI 태스크 등록·조회·취소"),
            BotCommand("merge", "태스크 브랜치 master 머지"),
            BotCommand("reject", "태스크 브랜치 삭제"),
            BotCommand("diff", "태스크 변경사항 보기"),
            BotCommand("ci_status", "최근 CI 실행 결과"),
            BotCommand("loop_status", "AI 루프 상태"),
            BotCommand("log", "loop.log 확인"),
        ])

    app.post_init = post_init

    log.info("개발해봇 시작")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
