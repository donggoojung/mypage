"""Playwright 브라우저 안티봇 우회 공통 설정 (PRD 2.2). abc_mart, 쿠팡 검색 등에서 재사용한다."""

import os

# 이 환경에는 Playwright 브라우저가 /opt/pw-browsers 에 사전 설치되어 있다.
# `playwright install`로 재다운로드하지 않고, 있으면 그 경로를 그대로 사용한다.
_PREINSTALLED_CHROMIUM = "/opt/pw-browsers/chromium"

# 실제 데스크톱 크롬 UA — 최신 버전 번호는 주기적으로 갱신 필요.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# navigator.webdriver 등 헤드리스 탐지 핵심 파라미터 제거.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""


def chromium_launch_kwargs(headless: bool) -> dict:
    kwargs = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if os.path.exists(_PREINSTALLED_CHROMIUM):
        kwargs["executable_path"] = _PREINSTALLED_CHROMIUM
    return kwargs


def new_context_kwargs() -> dict:
    return {
        "user_agent": USER_AGENT,
        "viewport": {"width": 1366, "height": 768},
        "locale": "ko-KR",
        "timezone_id": "Asia/Seoul",
    }
