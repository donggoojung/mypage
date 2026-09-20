from app.core.config import get_settings
from app.integrations.rpa.base import BaseRPAClient
from app.integrations.rpa.purchase_bot import MockRPAClient, PlaywrightRPAClient


def get_rpa_client(
    source_base_url: str = "", session_cookies: list[dict] | None = None, use_mock: bool | None = None
) -> BaseRPAClient:
    """설정(`USE_MOCK_RPA`)에 따라 Mock 또는 실제 Playwright RPA 클라이언트를 반환한다."""
    settings = get_settings()
    if use_mock is None:
        use_mock = settings.use_mock_rpa
    if use_mock:
        return MockRPAClient()
    return PlaywrightRPAClient(source_base_url=source_base_url, session_cookies=session_cookies or [], settings=settings)
