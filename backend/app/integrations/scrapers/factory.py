from functools import lru_cache

from app.core.config import get_settings
from app.integrations.scrapers.abc_mart import ABCMartScraper
from app.integrations.scrapers.base import BaseScraper
from app.integrations.scrapers.mock_scraper import MockScraper
from app.models.enums import SourcePlatform


@lru_cache
def get_scraper(source_platform: SourcePlatform = SourcePlatform.ABC_MART) -> BaseScraper:
    """설정(`USE_MOCK_SCRAPERS`)에 따라 Mock 또는 실제 소싱처 스크래퍼를 반환하는 팩토리."""
    settings = get_settings()
    if settings.use_mock_scrapers:
        return MockScraper()
    if source_platform == SourcePlatform.ABC_MART:
        return ABCMartScraper()
    raise NotImplementedError(f"{source_platform.value} 실제 스크래퍼는 아직 구현되지 않았습니다.")
