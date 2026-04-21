"""
smoke.py — 기본 스모크 테스트
AI 태스크가 앱을 생성했을 때 기본 동작 확인.
"""
from __future__ import annotations

from playwright.sync_api import Page, expect


def run(page: Page, base_url: str = "http://localhost:5000") -> None:
    """앱이 응답하는지 기본 확인."""
    page.goto(base_url, timeout=10_000)
    # 500 에러 페이지 아닌지 확인
    assert page.title() != "500 Internal Server Error", "앱이 500 에러 반환"
    # body가 비어있지 않은지 확인
    body = page.locator("body")
    expect(body).not_to_be_empty()
