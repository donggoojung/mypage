"""fix_barcode_info_for_draft_listings() 테스트.

2026-09-27 실전 테스트 중 발견: emptyBarcodeReason 버그가 있던 시절에 등록해둔
임시저장 상품 90개가 그대로 남아있어, 코드를 고친 뒤에도 이 상품들은 여전히
승인요청이 거부된다. "전체 갱신"(재크롤링+재등록/POST)을 쓰면 상품이 중복
생성되므로, 재크롤링 없이 기존 sellerProductId를 PUT으로 그대로 수정하는
전용 함수가 필요하다.
"""

from decimal import Decimal

from app.models.enums import ListingStatus, MarketType, SourcePlatform
from app.models.generated_asset import GeneratedAsset
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.services import product_pipeline
from app.services.product_pipeline import fix_barcode_info_for_draft_listings


class _NoCloseSessionWrapper:
    """테스트 전용 — with 블록이 끝나도 공유 db_session 픽스처를 닫지 않는다."""

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *args):
        pass


def _make_draft_listing(db_session, *, style_code: str, market_product_id: str) -> MarketListing:
    product = MasterProduct(style_code=style_code, brand_name="나이키", product_name="레볼루션 8")
    db_session.add(product)
    db_session.flush()

    db_session.add(
        GeneratedAsset(
            product_id=product.product_id,
            ai_thumbnail_url="https://mock-cdn.example.com/thumb.png",
            ai_detail_image_url="https://mock-cdn.example.com/detail.webp",
        )
    )
    db_session.add(
        SourceMapping(
            product_id=product.product_id,
            source_platform=SourcePlatform.ABC_MART,
            source_url=f"https://abcmart.a-rt.com/product?prdtNo={style_code}",
            source_price=71000,
            size_stock_json={"250": {"stock": 5, "is_sold_out": False}},
        )
    )
    listing = MarketListing(
        product_id=product.product_id,
        market_type=MarketType.COUPANG,
        market_product_id=market_product_id,
        selling_price=Decimal("106000"),
        status=ListingStatus.DRAFT,
    )
    db_session.add(listing)
    db_session.commit()
    return listing


async def _fake_predict_display_category_code(product_name: str) -> int:
    return 68010


def test_fixes_every_draft_listing_without_creating_duplicates(db_session, monkeypatch):
    _make_draft_listing(db_session, style_code="HJ9198", market_product_id="16395678159")
    _make_draft_listing(db_session, style_code="NC40160", market_product_id="16393192379")

    monkeypatch.setattr(product_pipeline, "SessionLocalSync", lambda: _NoCloseSessionWrapper(db_session))
    monkeypatch.setattr(product_pipeline, "predict_display_category_code", _fake_predict_display_category_code)

    captured_calls = []

    def _fake_register(**kwargs):
        captured_calls.append(kwargs)
        listing = (
            db_session.query(MarketListing)
            .filter_by(product_id=kwargs["product_id"], market_type=MarketType.COUPANG)
            .first()
        )
        listing.market_product_id = kwargs["existing_seller_product_id"]
        return listing

    monkeypatch.setattr(product_pipeline, "register_product_for_master_product", _fake_register)

    result = fix_barcode_info_for_draft_listings()

    assert result["total"] == 2
    assert len(result["succeeded"]) == 2
    assert result["failed"] == []
    # 새로 만드는(POST) 게 아니라 기존 sellerProductId를 그대로 넘겨 PUT 수정해야 한다.
    assert {c["existing_seller_product_id"] for c in captured_calls} == {"16395678159", "16393192379"}
    assert all(c["request_approval"] is False for c in captured_calls)
    # 여전히 상품이 2개뿐이어야 한다(중복 생성 없음).
    assert db_session.query(MarketListing).count() == 2


def test_skips_listing_without_size_stock_but_continues_others(db_session, monkeypatch):
    good = _make_draft_listing(db_session, style_code="HJ9198", market_product_id="16395678159")

    # size_stock이 없는 상품(예: 크롤링 이력이 아예 없는 이상 데이터) 하나 추가.
    broken_product = MasterProduct(style_code="BROKEN-1", brand_name="나이키", product_name="상품")
    db_session.add(broken_product)
    db_session.flush()
    db_session.add(
        MarketListing(
            product_id=broken_product.product_id,
            market_type=MarketType.COUPANG,
            market_product_id="99999999",
            selling_price=Decimal("50000"),
            status=ListingStatus.DRAFT,
        )
    )
    db_session.commit()

    monkeypatch.setattr(product_pipeline, "SessionLocalSync", lambda: _NoCloseSessionWrapper(db_session))
    monkeypatch.setattr(product_pipeline, "predict_display_category_code", _fake_predict_display_category_code)

    def _fake_register(**kwargs):
        listing = (
            db_session.query(MarketListing)
            .filter_by(product_id=kwargs["product_id"], market_type=MarketType.COUPANG)
            .first()
        )
        listing.market_product_id = kwargs["existing_seller_product_id"]
        return listing

    monkeypatch.setattr(product_pipeline, "register_product_for_master_product", _fake_register)

    result = fix_barcode_info_for_draft_listings()

    assert result["total"] == 2
    assert len(result["succeeded"]) == 1
    assert len(result["failed"]) == 1
    assert result["succeeded"][0]["market_product_id"] == good.market_product_id
