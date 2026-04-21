#!/usr/bin/env python3
"""
CI 결과를 Discord webhook으로 전송.
GitHub Actions step에서 직접 호출됨.
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path


def send_webhook(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "AI-Worker-CI/1.0",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 204):
            print(f"Discord webhook 응답: {resp.status}", file=sys.stderr)


def send_file(url: str, path: str, caption: str) -> None:
    """이미지 파일을 Discord에 업로드 (multipart)."""
    import mimetypes
    import uuid
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        file_data = f.read()
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    filename = Path(path).name
    payload_json = json.dumps({"content": caption}).encode()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="payload_json"\r\n\r\n'
    ).encode() + payload_json + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

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

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL 환경변수 없음", file=sys.stderr)
        sys.exit(1)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_url = f"https://github.com/{args.repo}/actions/runs/{args.run_id}"

    if args.status == "pass":
        payload = {
            "embeds": [{
                "title": "✅ 테스트 통과",
                "color": 0x00CC44,
                "fields": [{"name": "Run", "value": f"[#{args.run_id}]({run_url})", "inline": True}],
                "footer": {"text": ts},
            }]
        }
        send_webhook(webhook_url, payload)
    else:
        # 로그 읽기 (마지막 50줄)
        log_text = ""
        if args.log and Path(args.log).exists():
            lines = Path(args.log).read_text(errors="replace").splitlines()
            log_text = "\n".join(lines[-50:])

        payload = {
            "embeds": [{
                "title": "❌ 테스트 실패",
                "color": 0xFF0000,
                "fields": [
                    {"name": "Run", "value": f"[#{args.run_id}]({run_url})", "inline": True},
                    {"name": "로그 (마지막 50줄)", "value": f"```\n{log_text[:1000]}\n```", "inline": False},
                ],
                "footer": {"text": ts},
            }]
        }
        send_webhook(webhook_url, payload)

        # 스크린샷 전송
        for shot in (args.screenshot or []):
            if shot and Path(shot).exists():
                send_file(webhook_url, shot, f"📸 실패 스크린샷 ({ts})")


if __name__ == "__main__":
    main()
