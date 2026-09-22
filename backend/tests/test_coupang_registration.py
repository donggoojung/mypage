"""쿠팡 WING Open API 연동 검증.

Phase 3에서 만든 샘플 신발 데이터(AI 썸네일/상세페이지)를 불러와 쿠팡 상품 등록 페이로드를
생성하고, Mock 등록이 정상적으로 성공해 market_listings에 저장되는지 확인한다.
"""

from decimal import Decimal

import pytest

from app.integrations.markets.coupang import (
    CoupangProductInput,
    CoupangRegistrationError,
    CoupangWingClient,
    _generate_hmac_signature,
    build_seller_product_payload,
    predict_display_category_code,
    register_product_for_master_product,
    resolve_notice_info,
    resolve_seller_info,
    select_notice_category,
    sync_stock_and_price_to_coupang,
    sync_vendor_item_ids,
)
from app.models.enums import GenerationStatus, ListingStatus, MarketType
from app.models.generated_asset import GeneratedAsset
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct

SAMPLE_SIZE_STOCK = {
    "250": {"stock": 5, "is_sold_out": False},
    "260": {"stock": 0, "is_sold_out": True},
    "270": {"stock": 2, "is_sold_out": False},
}

SAMPLE_SELLER_INFO = {
    "delivery_company_code": "CJGLS",
    "return_center_code": "CENTER0001",
    "return_charge_name": "홍길동",
    "company_contact_number": "01012345678",
    "return_zip_code": "12345",
    "return_address": "서울시 강남구",
    "return_address_detail": "101호",
    "outbound_shipping_place_code": "12345678",
}


def _sample_input() -> CoupangProductInput:
    return CoupangProductInput(
        style_code="CW2288-111",
        brand_name="나이키",
        product_name="에어포스 1 '07 화이트",
        display_category_code=56137,  # 쿠팡 "운동화" 전시카테고리 예시 코드
        selling_price=Decimal("192834"),
        vendor_id="A00123456",
        vendor_user_id="test_vendor_user",
        thumbnail_image_url="https://mock-cdn.example.com/products/CW2288-111/thumbnail.png",
        detail_image_url="https://mock-cdn.example.com/products/CW2288-111/detail.webp",
        size_stock=SAMPLE_SIZE_STOCK,
        specs={"소재": "천연가죽", "색상": "화이트", "제조국": "베트남"},
    )


# --- 1. HMAC 서명 생성 검증 -------------------------------------------------


def test_hmac_signature_is_64_char_hex():
    signed_date, signature = _generate_hmac_signature(
        method="POST", path="/v2/providers/seller_api/apis/api/v1/marketplace/seller-products", secret_key="dummy-secret"
    )
    assert len(signature) == 64
    int(signature, 16)  # 유효한 16진수 문자열인지 확인 (아니면 ValueError)
    assert signed_date.endswith("Z")
    assert "T" in signed_date


def test_hmac_signature_changes_with_different_secret():
    _, sig1 = _generate_hmac_signature(method="POST", path="/some/path", secret_key="secret-a")
    _, sig2 = _generate_hmac_signature(method="POST", path="/some/path", secret_key="secret-b")
    assert sig1 != sig2


# --- 2. 페이로드 빌더 검증 ---------------------------------------------------


def test_build_seller_product_payload_structure():
    payload = build_seller_product_payload(_sample_input(), SAMPLE_SELLER_INFO)

    assert payload["displayCategoryCode"] == 56137
    assert payload["vendorId"] == "A00123456"
    assert "나이키" in payload["sellerProductName"]
    assert payload["deliveryChargeType"] == "FREE"
    assert payload["returnCenterCode"] == "CENTER0001"
    # 기본값은 반드시 False(임시저장) — 실계정 첫 테스트에서 실수로 승인요청이
    # 나가지 않도록 안전한 값을 기본으로 강제한다.
    assert payload["requested"] is False

    # 260 사이즈는 품절이라 items에서 제외되어야 한다.
    item_sizes = [item["itemName"] for item in payload["items"]]
    assert item_sizes == ["250", "270"]

    for item in payload["items"]:
        assert item["salePrice"] == 192834
        assert item["images"][0]["imageType"] == "REPRESENTATION"
        assert item["images"][1]["imageType"] == "DETAIL"
        assert item["externalVendorSku"].startswith("CW2288-111-")
        notice_categories = {n["noticeCategoryDetailName"] for n in item["notices"]}
        assert "소재" in notice_categories


def test_build_seller_product_payload_requests_approval_only_when_explicitly_true():
    data = _sample_input()
    data.request_approval = True
    payload = build_seller_product_payload(data, SAMPLE_SELLER_INFO)

    assert payload["requested"] is True


def test_build_seller_product_payload_uses_seo_title_when_provided():
    """Gemini가 만든 SEO 상품명이 있으면, 브랜드+상품명+품번을 이어붙이는 대신 그대로 써야 한다."""
    data = _sample_input()
    data.seo_title = "[나이키] 에어포스1 CW2288-111 화이트 클래식"
    payload = build_seller_product_payload(data, SAMPLE_SELLER_INFO)

    assert payload["sellerProductName"] == "[나이키] 에어포스1 CW2288-111 화이트 클래식"
    assert payload["displayProductName"] == "[나이키] 에어포스1 CW2288-111 화이트 클래식"


def test_build_seller_product_payload_raises_when_all_sizes_sold_out():
    data = _sample_input()
    data.size_stock = {"250": {"stock": 0, "is_sold_out": True}}

    with pytest.raises(ValueError, match="품절"):
        build_seller_product_payload(data, SAMPLE_SELLER_INFO)


# --- 3. Mock 클라이언트 등록 검증 -------------------------------------------


@pytest.mark.asyncio
async def test_mock_client_register_succeeds_and_validates_payload():
    client = CoupangWingClient(use_mock=True)
    payload = build_seller_product_payload(_sample_input(), SAMPLE_SELLER_INFO)

    response = await client.register_product(payload)

    assert response["code"] == "SUCCESS"
    assert isinstance(response["data"], int)


@pytest.mark.asyncio
async def test_mock_client_rejects_payload_missing_images():
    client = CoupangWingClient(use_mock=True)
    payload = build_seller_product_payload(_sample_input(), SAMPLE_SELLER_INFO)
    del payload["items"][0]["images"]

    with pytest.raises(CoupangRegistrationError, match="images"):
        await client.register_product(payload)


# --- 4. DB 연동 (master_products -> market_listings) 검증 -------------------


@pytest.fixture
def sample_product_with_asset(db_session):
    product = MasterProduct(
        style_code="CW2288-111",
        brand_name="나이키",
        product_name="에어포스 1 '07 화이트",
        raw_specs_json={"소재": "천연가죽", "색상": "화이트", "제조국": "베트남"},
    )
    db_session.add(product)
    db_session.flush()

    asset = GeneratedAsset(
        product_id=product.product_id,
        ai_thumbnail_url="https://mock-cdn.example.com/products/CW2288-111/thumbnail.png",
        ai_detail_image_url="https://mock-cdn.example.com/products/CW2288-111/detail.webp",
        exif_cleared=True,
        generation_status=GenerationStatus.COMPLETED,
    )
    db_session.add(asset)
    db_session.commit()
    return product


def test_register_product_for_master_product_creates_market_listing(sample_product_with_asset, db_session):
    listing = register_product_for_master_product(
        session=db_session,
        product_id=sample_product_with_asset.product_id,
        display_category_code=56137,
        selling_price=Decimal("192834"),
        size_stock=SAMPLE_SIZE_STOCK,
        use_mock=True,
        vendor_id="A00123456",
    )

    assert listing.market_type == MarketType.COUPANG
    # request_approval을 안 줬으니(기본 False) 실제 판매중이 아니라 임시저장 상태여야 한다.
    assert listing.status == ListingStatus.DRAFT
    assert listing.selling_price == Decimal("192834")
    assert listing.market_product_id is not None
    assert listing.registered_at is not None


def test_register_product_for_master_product_marks_active_when_approval_requested(
    sample_product_with_asset, db_session
):
    listing = register_product_for_master_product(
        session=db_session,
        product_id=sample_product_with_asset.product_id,
        display_category_code=56137,
        selling_price=Decimal("192834"),
        size_stock=SAMPLE_SIZE_STOCK,
        use_mock=True,
        vendor_id="A00123456",
        request_approval=True,
    )

    assert listing.status == ListingStatus.ACTIVE


def test_register_product_for_master_product_upserts_on_second_call(sample_product_with_asset, db_session):
    first = register_product_for_master_product(
        session=db_session,
        product_id=sample_product_with_asset.product_id,
        display_category_code=56137,
        selling_price=Decimal("192834"),
        size_stock=SAMPLE_SIZE_STOCK,
        use_mock=True,
        vendor_id="A00123456",
    )
    second = register_product_for_master_product(
        session=db_session,
        product_id=sample_product_with_asset.product_id,
        display_category_code=56137,
        selling_price=Decimal("199000"),
        size_stock=SAMPLE_SIZE_STOCK,
        use_mock=True,
        vendor_id="A00123456",
    )

    assert first.listing_id == second.listing_id  # 새로 생성이 아니라 기존 행을 갱신
    assert second.selling_price == Decimal("199000")


# --- 5. 출고지/반품지 자동조회 검증 -------------------------------------------


@pytest.mark.asyncio
async def test_resolve_seller_info_mock_returns_usable_places():
    seller_info = await resolve_seller_info(use_mock=True, vendor_id="A00123456")

    assert seller_info["outbound_shipping_place_code"] == "12345678"
    assert seller_info["return_center_code"] == "1000274596"
    assert seller_info["return_zip_code"] == "12345"
    assert seller_info["return_address"]
    assert seller_info["company_contact_number"]


@pytest.mark.asyncio
async def test_fetch_outbound_shipping_places_mock_marks_usable():
    client = CoupangWingClient(use_mock=True)
    places = await client.fetch_outbound_shipping_places("A00123456")

    assert places
    assert all("usable" in place for place in places)


def test_register_product_for_master_product_auto_resolves_seller_info(sample_product_with_asset, db_session):
    """seller_info를 생략하면 출고지/반품지 코드를 쿠팡 API로 자동 조회해서 채워야 한다."""
    listing = register_product_for_master_product(
        session=db_session,
        product_id=sample_product_with_asset.product_id,
        display_category_code=56137,
        selling_price=Decimal("192834"),
        size_stock=SAMPLE_SIZE_STOCK,
        use_mock=True,
        vendor_id="A00123456",
    )

    assert listing.status == ListingStatus.DRAFT


# --- 6. 카테고리 자동추천 검증 -------------------------------------------------


@pytest.mark.asyncio
async def test_predict_category_mock_returns_positive_category_id():
    client = CoupangWingClient(use_mock=True)
    result = await client.predict_category("나이키 에어포스 1 '07 화이트")

    assert result["predictedCategoryId"] > 0
    assert result["predictedCategoryName"]


@pytest.mark.asyncio
async def test_predict_display_category_code_returns_int():
    code = await predict_display_category_code("나이키 에어포스 1 '07 화이트", use_mock=True)

    assert isinstance(code, int)
    assert code > 0


# --- 7. 카테고리별 상품정보제공고시 항목 조회 검증 -------------------------------


def test_select_notice_category_prefers_shoe_category():
    metadata = {
        "noticeCategories": [
            {"noticeCategoryName": "기타재화", "noticeCategoryDetailNames": [{"name": "품명 및 모델명"}]},
            {"noticeCategoryName": "신발", "noticeCategoryDetailNames": [{"name": "소재"}, {"name": "색상"}]},
        ]
    }

    name, keys = select_notice_category(metadata)

    assert name == "신발"
    assert keys == ["소재", "색상"]


def test_select_notice_category_falls_back_to_first_when_no_shoe_match():
    metadata = {"noticeCategories": [{"noticeCategoryName": "기타재화", "noticeCategoryDetailNames": [{"name": "품명 및 모델명"}]}]}

    name, keys = select_notice_category(metadata)

    assert name == "기타재화"
    assert keys == ["품명 및 모델명"]


def test_select_notice_category_raises_when_no_categories():
    with pytest.raises(CoupangRegistrationError):
        select_notice_category({"noticeCategories": []})


@pytest.mark.asyncio
async def test_resolve_notice_info_mock_returns_shoe_category():
    name, keys = await resolve_notice_info(52021, use_mock=True)

    assert "신발" in name
    assert "소재" in keys


def test_register_product_for_master_product_requires_generated_asset(db_session):
    product = MasterProduct(style_code="NO-ASSET-001", brand_name="테스트", product_name="에셋없는상품")
    db_session.add(product)
    db_session.commit()

    with pytest.raises(ValueError, match="AI 생성 이미지"):
        register_product_for_master_product(
            session=db_session,
            product_id=product.product_id,
            display_category_code=56137,
            selling_price=Decimal("100000"),
            size_stock=SAMPLE_SIZE_STOCK,
            use_mock=True,
        )


# --- 8. 품절/가격 자동동기화(2단계 안전장치) 검증 -----------------------------------


@pytest.mark.asyncio
async def test_fetch_seller_product_mock_returns_items_with_vendor_item_id():
    client = CoupangWingClient(use_mock=True)

    detail = await client.fetch_seller_product("12345678")

    assert detail["items"]
    assert all("vendorItemId" in item for item in detail["items"])


@pytest.mark.asyncio
async def test_stop_and_resume_selling_item_mock_succeed():
    client = CoupangWingClient(use_mock=True)

    stop_response = await client.stop_selling_item("900001")
    resume_response = await client.resume_selling_item("900001")

    assert stop_response["code"] == "SUCCESS"
    assert resume_response["code"] == "SUCCESS"


@pytest.mark.asyncio
async def test_update_item_price_and_quantity_mock_succeed():
    client = CoupangWingClient(use_mock=True)

    price_response = await client.update_item_price("900001", 150000)
    quantity_response = await client.update_item_quantity("900001", 10)

    assert price_response["code"] == "SUCCESS"
    assert quantity_response["code"] == "SUCCESS"


@pytest.fixture
def sample_listing(sample_product_with_asset, db_session):
    listing = MarketListing(
        product_id=sample_product_with_asset.product_id,
        market_type=MarketType.COUPANG,
        market_product_id="12345678",
        selling_price=Decimal("192834"),
        status=ListingStatus.ACTIVE,
    )
    db_session.add(listing)
    db_session.commit()
    return listing


def test_sync_vendor_item_ids_mock_populates_mapping(sample_listing, db_session):
    mapping = sync_vendor_item_ids(session=db_session, listing=sample_listing, use_mock=True)

    assert mapping  # 비어있지 않아야 함
    db_session.refresh(sample_listing)
    assert sample_listing.vendor_item_ids_json == mapping


def test_sync_stock_and_price_stops_newly_sold_out_size(sample_listing, db_session):
    sample_listing.vendor_item_ids_json = {"250": "900001", "260": "900002"}
    db_session.commit()

    result = sync_stock_and_price_to_coupang(
        session=db_session,
        listing=sample_listing,
        old_size_stock={"250": {"is_sold_out": False}, "260": {"is_sold_out": False}},
        new_size_stock={"250": {"is_sold_out": False}, "260": {"is_sold_out": True}},
        use_mock=True,
    )

    assert result["stopped"] == ["260"]
    assert result["resumed"] == []


def test_sync_stock_and_price_resumes_restocked_size(sample_listing, db_session):
    sample_listing.vendor_item_ids_json = {"250": "900001"}
    db_session.commit()

    result = sync_stock_and_price_to_coupang(
        session=db_session,
        listing=sample_listing,
        old_size_stock={"250": {"is_sold_out": True}},
        new_size_stock={"250": {"is_sold_out": False}},
        use_mock=True,
    )

    assert result["resumed"] == ["250"]
    assert result["stopped"] == []


def test_sync_stock_and_price_updates_price_when_changed(sample_listing, db_session):
    sample_listing.vendor_item_ids_json = {"250": "900001"}
    sample_listing.selling_price = Decimal("100000")
    db_session.commit()

    result = sync_stock_and_price_to_coupang(
        session=db_session,
        listing=sample_listing,
        old_size_stock={"250": {"is_sold_out": False}},
        new_size_stock={"250": {"is_sold_out": False}},
        new_selling_price=Decimal("150000"),
        use_mock=True,
    )

    assert result["price_updated"] is True
    db_session.refresh(sample_listing)
    assert sample_listing.selling_price == Decimal("150000")


def test_sync_stock_and_price_skips_when_vendor_item_ids_empty(sample_listing, db_session):
    """승인 전(DRAFT)이라 vendorItemId가 없으면 아무것도 안 하고 조용히 건너뛰어야 한다."""
    assert sample_listing.vendor_item_ids_json == {}

    result = sync_stock_and_price_to_coupang(
        session=db_session,
        listing=sample_listing,
        old_size_stock={"250": {"is_sold_out": False}},
        new_size_stock={"250": {"is_sold_out": True}},
        new_selling_price=Decimal("150000"),
        use_mock=True,
    )

    assert result["stopped"] == []
    assert result["skipped_reason"] is not None
