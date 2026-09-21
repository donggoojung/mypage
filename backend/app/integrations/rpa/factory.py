import json
from pathlib import Path

from app.core.config import get_settings
from app.integrations.rpa.base import BaseRPAClient
from app.integrations.rpa.purchase_bot import MockRPAClient, PlaywrightRPAClient


def _load_saved_session_cookies(session_file: str) -> list[dict]:
    """scripts/save_abc_mart_session.py로 저장해둔 로그인 쿠키 파일을 읽는다.

    파일이 없으면 빈 목록을 반환한다 — 이 경우 PlaywrightRPAClient.purchase_order가
    "세션이 없다"는 명확한 에러를 내므로, 여기서는 조용히 넘어가도 안전하다.
    """
    path = Path(session_file)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def get_rpa_client(
    source_base_url: str = "", session_cookies: list[dict] | None = None, use_mock: bool | None = None
) -> BaseRPAClient:
    """설정(`USE_MOCK_RPA`)에 따라 Mock 또는 실제 Playwright RPA 클라이언트를 반환한다."""
    settings = get_settings()
    if use_mock is None:
        use_mock = settings.use_mock_rpa
    if use_mock:
        return MockRPAClient()
    if session_cookies is None:
        session_cookies = _load_saved_session_cookies(settings.abc_mart_session_file)
    return PlaywrightRPAClient(source_base_url=source_base_url, session_cookies=session_cookies, settings=settings)
