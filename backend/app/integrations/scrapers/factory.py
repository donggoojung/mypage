from functools import lru_cache

from app.core.config import get_settings
from app.integrations.scrapers.abc_mart import ABCMartScraper
from app.integrations.scrapers.base import BaseScraper
from app.integrations.scrapers.mock_scraper import MockScraper
from app.integrations.scrapers.musinsa import MusinsaScraper
from app.models.enums import SourcePlatform


@lru_cache
def get_scraper(source_platform: SourcePlatform = SourcePlatform.ABC_MART) -> BaseScraper:
    """설정(`USE_MOCK_SCRAPERS`)에 따라 Mock 또는 실제 소싱처 스크래퍼를 반환하는 팩토리."""
    settings = get_settings()
    if settings.use_mock_scrapers:
        return MockScraper()
    if source_platform == SourcePlatform.ABC_MART:
        return ABCMartScraper()
    if source_platform == SourcePlatform.MUSINSA:
        # 실사이트 미검증 스켈레톤 — 실제 상품에 SourceMapping(MUSINSA)을 등록하기
        # 전까지는 find_cheapest_source가 이 스크래퍼를 실제로 호출하지 않는다.
        return MusinsaScraper()
    raise NotImplementedError(f"{source_platform.value} 실제 스크래퍼는 아직 구현되지 않았습니다.")
