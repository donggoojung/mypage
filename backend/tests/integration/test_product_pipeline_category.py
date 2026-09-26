"""product_pipeline.run_pipeline_for_url()의 [5/5] 쿠팡 등록 전시카테고리 검증 로직 테스트.

2026-09-22 실사용 중 발견: 쿠팡 카테고리 자동추천 API가 상품명만 보고 신발과 무관한
카테고리(예: "소형전자")를 잘못 고르는 경우가 있었고, 그 카테고리엔 "신발사이즈" 속성이
없어 쿠팡이 사이즈별 옵션을 구분 못 해 "중복된 옵션값이 있습니다"로 등록을 거부했다.
이 테스트는 추천된 카테고리의 고시정보에 "신발"이 없으면 검증된 기본 신발 카테고리로
되돌리는 안전장치가 실제로 동작하는지 확인한다. ABC마트 크롤링/AI 이미지 생성/쿠팡
등록 자체는 monkeypatch로 대체해서, 카테고리 결정 로직만 분리해서 검증한다.
"""

from decimal import Decimal

import pytest

from app.integrations.scrapers.base import ScrapedProduct
from app.models.enums import ListingStatus
from app.services import product_pipeline
from app.services.product_pipeline import DEFAULT_DISPLAY_CATEGORY_CODE, PipelineOptions, run_pipeline_for_url


class _FakeListing:
    listing_id = 1
    market_product_id = "12345"
    status = ListingStatus.DRAFT
    selling_price = Decimal("63000")


@pytest.fixture
def fake_scraped_product():
    return ScrapedProduct(
        style_code="CW2288-111",
        brand_name="나이키",
        product_name="에어포스 1 '07 화이트",
        source_url="https://abcmart.a-rt.com/product?prdtNo=1010120440",
        price=39000.0,
        image_url="",  # AI 이미지 생성 단계를 건너뛰게 해서 이 테스트 범위를 카테고리 로직으로 좁힌다.
        size_stock={"250": {"stock": 5, "is_sold_out": False}},
    )


def _patch_common(monkeypatch, fake_scraped_product):
    async def _fake_fetch_product_by_url(self, url):
        return fake_scraped_product

    monkeypatch.setattr("app.integrations.scrapers.abc_mart.ABCMartScraper.fetch_product_by_url", _fake_fetch_product_by_url)

    captured_category_code = {}

    def _fake_register_coupang_sync(
        product_id, display_category_code, selling_price, size_stock, request_approval, *args, **kwargs
    ):
        captured_category_code["value"] = display_category_code
        return _FakeListing()

    monkeypatch.setattr(product_pipeline, "_register_coupang_sync", _fake_register_coupang_sync)
    return captured_category_code


async def test_wrong_category_prediction_falls_back_to_default_shoe_category(db_session, monkeypatch):
    """자동추천 카테고리의 고시정보에 "신발"이 없으면 기본 신발 카테고리로 되돌아가야 한다."""
    captured = _patch_common(monkeypatch, fake_scraped_product=ScrapedProduct(
        style_code="CW2288-111",
        brand_name="나이키",
        product_name="에어포스 1 '07 화이트",
        source_url="https://abcmart.a-rt.com/product?prdtNo=1010120440",
        price=39000.0,
        image_url="",
        size_stock={"250": {"stock": 5, "is_sold_out": False}},
    ))

    async def _fake_predict(product_name):
        return 99999  # 신발과 무관한(예: 전자제품) 잘못된 예측값

    async def _fake_resolve_notice_info(display_category_code):
        assert display_category_code == 99999
        return "소형전자(MP3/전자사전 등)", ["품명 및 모델명", "정격전압"]

    monkeypatch.setattr(product_pipeline, "predict_display_category_code", _fake_predict)
    monkeypatch.setattr(product_pipeline, "resolve_notice_info", _fake_resolve_notice_info)

    await run_pipeline_for_url("https://abcmart.a-rt.com/product?prdtNo=1010120440")

    assert captured["value"] == DEFAULT_DISPLAY_CATEGORY_CODE


async def test_correct_shoe_category_prediction_is_kept(db_session, monkeypatch):
    """자동추천 카테고리의 고시정보에 "신발"이 있으면 그 값을 그대로 써야 한다(불필요한 override 금지)."""
    captured = _patch_common(monkeypatch, fake_scraped_product=ScrapedProduct(
        style_code="CW2288-111",
        brand_name="나이키",
        product_name="에어포스 1 '07 화이트",
        source_url="https://abcmart.a-rt.com/product?prdtNo=1010120440",
        price=39000.0,
        image_url="",
        size_stock={"250": {"stock": 5, "is_sold_out": False}},
    ))

    async def _fake_predict(product_name):
        return 56137

    async def _fake_resolve_notice_info(display_category_code):
        assert display_category_code == 56137
        return "구두/신발", ["제품의 주소재", "색상", "치수"]

    monkeypatch.setattr(product_pipeline, "predict_display_category_code", _fake_predict)
    monkeypatch.setattr(product_pipeline, "resolve_notice_info", _fake_resolve_notice_info)

    await run_pipeline_for_url("https://abcmart.a-rt.com/product?prdtNo=1010120440")

    assert captured["value"] == 56137


async def test_manual_category_override_skips_validation(db_session, monkeypatch):
    """사용자가 고급 옵션에서 카테고리를 직접 지정하면, 자동추천/검증 로직 자체를 타지 않고 그대로 써야 한다."""
    captured = _patch_common(monkeypatch, fake_scraped_product=ScrapedProduct(
        style_code="CW2288-111",
        brand_name="나이키",
        product_name="에어포스 1 '07 화이트",
        source_url="https://abcmart.a-rt.com/product?prdtNo=1010120440",
        price=39000.0,
        image_url="",
        size_stock={"250": {"stock": 5, "is_sold_out": False}},
    ))

    async def _unexpected_call(*args, **kwargs):
        raise AssertionError("수동 지정 시에는 자동추천/검증 API를 호출하면 안 됩니다.")

    monkeypatch.setattr(product_pipeline, "predict_display_category_code", _unexpected_call)
    monkeypatch.setattr(product_pipeline, "resolve_notice_info", _unexpected_call)

    await run_pipeline_for_url(
        "https://abcmart.a-rt.com/product?prdtNo=1010120440",
        options=PipelineOptions(display_category_code=12345),
    )

    assert captured["value"] == 12345
