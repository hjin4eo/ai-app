#!/usr/bin/env python3
"""
텔레그램/디스코드 요청 → AI 코드 생성 → commit/push
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_cfg_path = Path(__file__).parent.parent / "config.yaml"
with open(_cfg_path) as f:
    CFG = yaml.safe_load(f)

_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

WORK_DIR = Path(__file__).parent.parent.resolve()


def handle_request(request: str) -> str:
    """
    사용자 요청을 AI로 처리 후 코드를 작성하고 push.
    결과 메시지를 반환.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from models import get_model

    try:
        model = get_model(CFG)
    except Exception as e:
        return f"❌ 모델 초기화 실패: {e}"

    log.info("AI에게 요청 전달: %s", request[:80])
    try:
        files = model.generate_code(request)
    except Exception as e:
        return f"❌ AI 코드 생성 실패: {e}"

    if not files:
        return "❌ AI가 파일을 생성하지 않았습니다."

    written = []
    for rel_path, content in files.items():
        target = WORK_DIR / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel_path)
        log.info("파일 작성: %s", rel_path)

    try:
        subprocess.run(["git", "add", "--"] + written, cwd=WORK_DIR, check=True)
        short_req = request[:50].replace("\n", " ")
        subprocess.run(
            ["git", "commit", "-m", f"feat: {short_req}"],
            cwd=WORK_DIR, check=True,
        )
        token = CFG.get("github", {}).get("token") or os.environ.get("GITHUB_TOKEN", "")
        repo = CFG.get("github", {}).get("repo", "")
        if token and repo:
            remote = f"https://{token}@github.com/{repo}.git"
            subprocess.run(["git", "push", remote, "master"], cwd=WORK_DIR, check=True)
        else:
            subprocess.run(["git", "push"], cwd=WORK_DIR, check=True)
    except subprocess.CalledProcessError as e:
        return f"❌ commit/push 실패: {e}"

    files_str = "\n".join(f"  • {f}" for f in written)
    return f"✅ 완료! {len(written)}개 파일 생성 후 push:\n{files_str}\n\nCI 결과는 Discord/Telegram으로 알림 옵니다."
