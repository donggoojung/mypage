import json
from pathlib import Path

from app.core.config import get_settings
from app.integrations.rpa.base import BaseRPAClient
from app.integrations.rpa.musinsa_purchase_bot import MusinsaRPAClient
from app.integrations.rpa.purchase_bot import MockRPAClient, PlaywrightRPAClient
from app.models.enums import SourcePlatform


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
    source_base_url: str = "",
    session_cookies: list[dict] | None = None,
    use_mock: bool | None = None,
    source_platform: SourcePlatform = SourcePlatform.ABC_MART,
) -> BaseRPAClient:
    """설정(`USE_MOCK_RPA`)과 소싱처(`source_platform`)에 따라 Mock 또는 실제 RPA 클라이언트를 반환한다."""
    settings = get_settings()
    if use_mock is None:
        use_mock = settings.use_mock_rpa
    if use_mock:
        return MockRPAClient()

    if source_platform == SourcePlatform.MUSINSA:
        # 실사이트 미검증 스켈레톤 — 실제 상품에 SourceMapping(MUSINSA)을 등록하기
        # 전까지는 order_processor.py가 이 클라이언트를 실제로 호출하지 않는다.
        if session_cookies is None:
            session_cookies = _load_saved_session_cookies(settings.musinsa_session_file)
        return MusinsaRPAClient(source_base_url=source_base_url, session_cookies=session_cookies, settings=settings)

    if session_cookies is None:
        session_cookies = _load_saved_session_cookies(settings.abc_mart_session_file)
    return PlaywrightRPAClient(source_base_url=source_base_url, session_cookies=session_cookies, settings=settings)
