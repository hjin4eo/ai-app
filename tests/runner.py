#!/usr/bin/env python3
"""
Playwright headed 테스트 실행기.
실패 시 /tmp/test_fail_<timestamp>.png 스크린샷 저장.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, expect


def screenshot_on_fail(page: Page, name: str) -> str:
    path = f"/tmp/test_fail_{name}_{datetime.now().strftime('%H%M%S')}.png"
    page.screenshot(path=path, full_page=True)
    print(f"[SCREENSHOT] {path}")
    return path


def run_tests(page: Page) -> None:
    """
    테스트를 여기에 작성하세요.
    앱/게임이 결정되면 이 함수를 교체합니다.
    """
    # ── 예시: 로컬 앱 테스트 ──────────────────────────────────────────────────
    # page.goto("http://localhost:3000")
    # expect(page.locator("h1")).to_be_visible()

    # ── 현재: 샘플 (항상 통과) ────────────────────────────────────────────────
    page.goto("about:blank")
    assert page.title() == "", f"예상치 못한 타이틀: {page.title()}"
    print("[PASS] 샘플 테스트 통과")


def main() -> int:
    with sync_playwright() as pw:
        headless = os.environ.get("HEADLESS", "0") == "1"
        browser = pw.chromium.launch(
            headless=headless,
            slow_mo=0 if headless else 500,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            record_video_dir="/tmp/videos/" if Path("/tmp/videos").exists() else None,
        )
        page = context.new_page()

        try:
            run_tests(page)
            return 0
        except Exception as e:
            print(f"[FAIL] {e}", file=sys.stderr)
            screenshot_on_fail(page, "test")
            return 1
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
