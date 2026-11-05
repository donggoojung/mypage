"""스마트스토어 등록 점검·요청 내용 만들기·네이버 클라이언트 (실제 네이버에는 아무것도 보내지 않는다)."""

import base64
from types import SimpleNamespace

import bcrypt
import httpx
import pytest

from app.integrations.markets.naver_commerce import (
    MockSmartstoreClient,
    NaverCommerceClient,
    NaverCommerceError,
    make_client_secret_sign,
)
from app.services import agri_pricing as pricing
from app.services import agri_smartstore as ss


def _settings(**smartstore_overrides):
    settings = pricing.merge_settings(None)
    settings["cs_phone"] = "1588-0000"
    settings["smartstore"]["leaf_category_ids"]["fruit"] = "50000100"
    settings["smartstore"]["leaf_category_ids"]["rice"] = "50000200"
    settings["smartstore"].update(smartstore_overrides)
    return settings


def _option(label, supply, ship=0, sale=None, soldout=False, code=None):
    return SimpleNamespace(
        label=label,
        supply_price=supply,
        shipping_fee=ship,
        sale_price=sale,
        is_soldout=soldout,
        supplier_code=code,
        weight=None,
        count=None,
        grade=None,
    )


def _product(**overrides):
    data = dict(
        product_id=7,
        name="충주 사과",
        category="fruit",
        origin="국산(충북 충주)",
        producer="충주농원",
        storage="냉장 보관",
        packing_info=None,
        variety=None,
        harvest_year=None,
        grade=None,
        image_url="https://example.com/apple.jpg",
        status="ready",
        options=[_option("3kg", 12000, code="A3"), _option("5kg", 18000, code="A5")],
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def test_complete_product_has_no_problems():
    assert ss.collect_problems(_product(), _settings()) == []


def test_missing_info_is_listed_one_by_one():
    product = _product(origin=None, producer=None, image_url=None)
    settings = pricing.merge_settings(None)  # 카테고리 번호·전화번호 없음
    text = " ".join(ss.collect_problems(product, settings))
    for needle in ("원산지", "생산자", "대표 사진", "카테고리 번호", "고객센터 전화번호"):
        assert needle in text


def test_rice_requires_grain_label_fields():
    product = _product(category="rice")
    text = " ".join(ss.collect_problems(product, _settings()))
    assert "품종" in text and "생산연도" in text and "등급" in text
    full = _product(category="rice", variety="추청", harvest_year="2026", grade="특")
    assert ss.collect_problems(full, _settings()) == []


def test_price_below_min_profit_blocks_registration():
    product = _product(options=[_option("3kg", 12000, sale=12500)])
    problems = ss.collect_problems(product, _settings())
    assert any("최소 이익" in p for p in problems)


def test_soldout_option_is_left_out_and_all_soldout_blocks():
    product = _product(options=[_option("3kg", 12000), _option("5kg", 18000, soldout=True)])
    payload = ss.build_payload(product, _settings(), ["https://img/1.jpg"])
    assert "optionInfo" not in payload["originProduct"]["detailAttribute"]  # 남은 옵션이 하나뿐
    all_out = _product(options=[_option("3kg", 12000, soldout=True)])
    assert any("판매할 수 있는 옵션이 없습니다" in p for p in ss.collect_problems(all_out, _settings()))


def test_option_price_spread_too_large_is_blocked():
    product = _product(options=[_option("작은 것", 5000), _option("아주 큰 것", 40000)])
    assert any("너무 비쌉니다" in p for p in ss.collect_problems(product, _settings()))


def test_payload_uses_cheapest_option_as_base_price():
    settings = _settings()
    product = _product()
    rows = ss.option_prices(product, settings)
    payload = ss.build_payload(product, settings, ["https://img/1.jpg", "https://img/2.jpg"])
    origin = payload["originProduct"]
    base = min(r["price"] for r in rows)
    assert origin["salePrice"] == base
    combos = origin["detailAttribute"]["optionInfo"]["optionCombinations"]
    assert [c["optionName1"] for c in combos] == ["3kg", "5kg"]
    assert combos[0]["price"] == 0
    assert combos[1]["price"] == rows[1]["price"] - base
    assert combos[1]["sellerManagerCode"] == "A5"
    assert origin["images"]["representativeImage"]["url"] == "https://img/1.jpg"
    assert origin["images"]["optionalImages"] == [{"url": "https://img/2.jpg"}]
    assert origin["leafCategoryId"] == "50000100"


def test_payload_basics_and_safety_defaults():
    payload = ss.build_payload(_product(), _settings(), ["https://img/1.jpg"])
    origin = payload["originProduct"]
    assert origin["detailAttribute"]["taxType"] == "DUTYFREE"
    assert origin["deliveryInfo"]["deliveryFee"]["deliveryFeeType"] == "FREE"
    assert origin["detailAttribute"]["originAreaInfo"]["content"] == "국산(충북 충주)"
    assert "원산지" in origin["detailContent"]
    # 처음에는 전시 중지로 올린다
    assert payload["smartstoreChannelProduct"]["channelProductDisplayStatusType"] == "SUSPENSION"
    on = ss.build_payload(_product(), _settings(display_on_register=True), ["https://img/1.jpg"])
    assert on["smartstoreChannelProduct"]["channelProductDisplayStatusType"] == "ON"


def test_detail_html_escapes_product_text():
    product = _product(name="<script>alert(1)</script> 사과")
    html_text = ss.build_payload(product, _settings(), ["https://img/1.jpg"])["originProduct"]["detailContent"]
    assert "<script>" not in html_text


# ---------------------------------------------------------------- 네이버 클라이언트


def test_client_secret_sign_can_be_verified_with_bcrypt():
    secret = bcrypt.gensalt().decode()
    sign = make_client_secret_sign("my-id", secret, 1730000000000)
    hashed = base64.b64decode(sign)
    assert bcrypt.checkpw(b"my-id_1730000000000", hashed)


async def test_mock_client_records_calls():
    client = MockSmartstoreClient()
    url = await client.upload_image("https://example.com/a.jpg")
    assert url.startswith("https://shop-phinf.pstatic.net/")
    result = await client.register_product({"x": 1})
    assert result.origin_product_no.startswith("MOCK-")
    assert client.registered == [{"x": 1}]


def _real_client(handler):
    secret = bcrypt.gensalt().decode()
    return NaverCommerceClient("id", secret, transport=httpx.MockTransport(handler))


async def test_real_client_gets_token_once_and_registers():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/external/v1/oauth2/token":
            body = request.content.decode()
            assert "grant_type=client_credentials" in body and "client_secret_sign=" in body
            return httpx.Response(200, json={"access_token": "TOKEN", "expires_in": 10800})
        assert request.headers["authorization"] == "Bearer TOKEN"
        if request.url.path == "/external/v2/products":
            return httpx.Response(200, json={"originProductNo": 123, "smartstoreChannelProductNo": 456})
        return httpx.Response(404)

    client = _real_client(handler)
    first = await client.register_product({"a": 1})
    second = await client.register_product({"a": 2})
    assert (first.origin_product_no, first.channel_product_no) == ("123", "456")
    assert second.origin_product_no == "123"
    assert [c for c in calls if c[1].endswith("/token")] == [("POST", "/external/v1/oauth2/token")]


async def test_real_client_upload_downloads_then_uploads():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(200, content=b"\xff\xd8jpegbytes", headers={"content-type": "image/jpeg"})
        if request.url.path == "/external/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        if request.url.path == "/external/v1/product-images/upload":
            assert b"imageFiles" in request.content
            return httpx.Response(200, json={"images": [{"url": "https://shop-phinf.pstatic.net/x.jpg"}]})
        return httpx.Response(404)

    url = await _real_client(handler).upload_image("https://example.com/a.jpg")
    assert url == "https://shop-phinf.pstatic.net/x.jpg"


async def test_real_client_turns_rejection_into_readable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/external/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(
            400,
            json={"message": "요청이 올바르지 않습니다", "invalidInputs": [{"name": "leafCategoryId", "message": "없는 카테고리"}]},
        )

    with pytest.raises(NaverCommerceError) as exc:
        await _real_client(handler).register_product({})
    assert "leafCategoryId" in str(exc.value) and "없는 카테고리" in str(exc.value)


async def test_real_client_login_failure_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "인증 실패"})

    with pytest.raises(NaverCommerceError) as exc:
        await _real_client(handler).register_product({})
    assert "로그인" in str(exc.value)


def test_real_client_requires_keys():
    with pytest.raises(ValueError):
        NaverCommerceClient("", "")
