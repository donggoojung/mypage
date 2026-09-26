"""지식재산권 침해 신고가 잦은 브랜드를 등록 전에 걸러내는 필터 테스트 (PRD 9.1).

'섹션 일괄 등록'이 카테고리 전체를 긁을 때 특정 브랜드가 섞여 들어가는 걸
막기 위한 안전장치 — settings.blacklisted_brands(쉼표 구분)에 값이 있으면
크롤링 직후(DB 저장/AI이미지/쿠팡등록 전) 즉시 걸러내야 한다.
"""

import pytest

from app.core.config import get_settings
from app.integrations.scrapers.abc_mart import ABCMartScraper
from app.integrations.scrapers.base import ScrapedProduct
from app.services.product_pipeline import PipelineError, _is_blacklisted_brand, run_pipeline_for_url


def test_is_blacklisted_brand_matches_case_insensitively(monkeypatch):
    monkeypatch.setattr(get_settings(), "blacklisted_brands", "나이키,조던")
    assert _is_blacklisted_brand("나이키", "에어포스 1") is True


def test_is_blacklisted_brand_matches_via_product_name(monkeypatch):
    # ABC마트는 브랜드가 상품명에도 같이 표기되는 경우가 있어 상품명도 같이 검사한다.
    monkeypatch.setattr(get_settings(), "blacklisted_brands", "나이키,조던")
    assert _is_blacklisted_brand("", "나이키 에어포스 1") is True


def test_is_blacklisted_brand_returns_false_when_not_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "blacklisted_brands", "")
    assert _is_blacklisted_brand("나이키", "에어포스 1") is False


def test_is_blacklisted_brand_returns_false_for_unrelated_brand(monkeypatch):
    monkeypatch.setattr(get_settings(), "blacklisted_brands", "나이키,조던")
    assert _is_blacklisted_brand("뉴발란스", "993") is False


async def test_run_pipeline_for_url_raises_before_any_registration_for_blacklisted_brand(monkeypatch):
    monkeypatch.setattr(get_settings(), "blacklisted_brands", "나이키, 조던")

    async def _fake_fetch_product_by_url(self, product_url):
        return ScrapedProduct(
            style_code="CW2288-111",
            brand_name="나이키",
            product_name="에어포스 1 '07 화이트",
            source_url=product_url,
            price=139000.0,
        )

    monkeypatch.setattr(ABCMartScraper, "fetch_product_by_url", _fake_fetch_product_by_url)

    with pytest.raises(PipelineError, match="금지 브랜드"):
        await run_pipeline_for_url("https://abcmart.a-rt.com/product?prdtNo=1")
