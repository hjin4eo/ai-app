#!/usr/bin/env python3
"""
CI 결과를 Telegram으로 전송.
GitHub Actions step에서 직접 호출됨.
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path


def send_message(token: str, chat_id: str, text: str, parse_mode: str = "Markdown") -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        if not result.get("ok"):
            print(f"Telegram 전송 실패: {result}", file=sys.stderr)


def send_photo(token: str, chat_id: str, path: str, caption: str) -> None:
    """이미지 파일을 Telegram에 전송 (multipart)."""
    import mimetypes
    import uuid
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        file_data = f.read()
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    filename = Path(path).name

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n'
        f"{caption}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    urllib.request.urlopen(req, timeout=15)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", choices=["pass", "fail"], required=True)
    parser.add_argument("--log", default=None)
    parser.add_argument("--screenshot", nargs="*", default=[])
    parser.add_argument("--run-id", default="")
    parser.add_argument("--repo", default="")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 환경변수 없음", file=sys.stderr)
        sys.exit(1)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_url = f"https://github.com/{args.repo}/actions/runs/{args.run_id}"

    if args.status == "pass":
        text = (
            f"*✅ 테스트 통과*\n\n"
            f"Run: [#{args.run_id}]({run_url})\n"
            f"시각: {ts}"
        )
        send_message(token, chat_id, text)
    else:
        log_text = ""
        if args.log and Path(args.log).exists():
            lines = Path(args.log).read_text(errors="replace").splitlines()
            log_text = "\n".join(lines[-30:])

        text = (
            f"*❌ 테스트 실패*\n\n"
            f"Run: [#{args.run_id}]({run_url})\n"
            f"시각: {ts}\n\n"
            f"*로그 (마지막 30줄):*\n```\n{log_text[:3000]}\n```"
        )
        send_message(token, chat_id, text)

        for shot in (args.screenshot or []):
            if shot and Path(shot).exists():
                send_photo(token, chat_id, shot, f"📸 실패 스크린샷 ({ts})")


if __name__ == "__main__":
    main()
