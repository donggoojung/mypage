"""토스쇼핑 연동: 등록 전 점검, 요청 내용 만들기, 실제 클라이언트가 보내는 모양 (토스쇼핑에는 아무것도 보내지 않는다)."""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.markets.toss import MockTossClient, TossClient, TossError, _parse_order
from app.services import agri_pricing as pricing
from app.services import agri_toss as toss
from app.services import agri_toss_orders as toss_orders


def _settings():
    settings = pricing.merge_settings(None)
    settings["cs_phone"] = "1588-0000"
    box = settings["toss"]
    box.update(notice_category_code="AGRI_FOOD", exchange_refund_location_id="2001", delivery_location_id="1001")
    box["leaf_category_ids"]["fruit"] = "101"
    box["sales_option_keys"]["fruit"] = "중량"
    return settings


def _option(option_id, label, supply):
    return SimpleNamespace(
        option_id=option_id, label=label, supply_price=supply, shipping_fee=0, sale_price=None,
        is_soldout=False, supplier_code=None, weight=None, count=None, grade=None,
    )


def _product(**overrides):
    data = dict(
        product_id=7, name="충주 사과 (특)", category="fruit", origin="국산(충북 충주)", producer="충주농원",
        storage="냉장 보관", packing_info=None, variety=None, harvest_year=None, grade=None,
        image_url="https://example.com/apple.jpg", status="ready",
        options=[_option(11, "3kg", 12000), _option(12, "5kg", 18000)],
    )
    data.update(overrides)
    return SimpleNamespace(**data)


NOTICE = [
    {"id": 901, "title": "1. 품목 또는 명칭"}, {"id": 902, "title": "2. 포장단위별 내용물의 용량(중량), 수량, 크기"},
    {"id": 903, "title": "3. 생산자, 수입자"}, {"id": 904, "title": "4. 원산지"},
    {"id": 905, "title": "5. 제조연월일, 소비기한 또는 품질유지기한"}, {"id": 908, "title": "8. 보관방법 또는 취급방법"},
    {"id": 910, "title": "10. 소비자 상담 관련 전화번호"}, {"id": 999, "title": "11. 알 수 없는 항목"},
]


def test_complete_product_has_no_problems():
    assert toss.collect_problems(_product(), _settings()) == []


def test_blank_settings_and_bad_name_are_listed():
    text = " ".join(toss.collect_problems(_product(name="사과 100% 특가!"), pricing.merge_settings(None)))
    for needle in ("상품명", "카테고리 번호", "구매옵션 이름", "고시 카테고리 코드", "교환반품지", "고객센터"):
        assert needle in text


def test_rice_needs_label_fields_and_low_price_blocks():
    assert "품종" in " ".join(toss.collect_problems(_product(category="rice"), _settings()))
    low = _product(options=[SimpleNamespace(**{**_option(11, "3kg", 12000).__dict__, "sale_price": 12500})])
    assert any("최소 이익" in p for p in toss.collect_problems(low, _settings()))


def test_payload_follows_the_v3_spec():
    payload = toss.build_payload(_product(), _settings(), NOTICE)
    assert payload["productName"] == "충주 사과 (특)" and payload["productManagementCode"] == "agri-7"
    assert payload["isTaxFree"] is True and payload["leafCategoryId"] == 101
    first, second = payload["items"]
    assert first["salesOptions"] == [{"key": "중량", "value": "3kg"}] and first["itemManagementCode"] == "agri-11"
    assert first["isMainOption"] is True and second["isMainOption"] is False
    assert first["originPrice"] == first["regularSalePrice"] < second["regularSalePrice"]
    delivery = payload["deliveryPolicy"]
    assert delivery["deliveryFeeType"] == "FREE" and delivery["deliveryFee"] == 0 and delivery["preparationDays"] == 3
    assert delivery["deliveryLocationId"] == 1001 and delivery["deliveryBlockedRegionPolicyId"] == 1
    assert payload["exchangeReturnPolicy"]["exchangeRefundLocationId"] == 2001
    assert "신선식품" in payload["exchangeReturnPolicy"]["applicationTermDescription"]
    assert payload["productThumbnails"] == [{"productThumbnailUrl": "https://example.com/apple.jpg"}]
    assert payload["productMandatoryNotice"]["mandatoryNoticeCode"] == "AGRI_FOOD"


def test_notice_items_are_filled_by_title_and_unknown_ones_point_to_the_detail_page():
    items = {i["id"]: i["content"] for i in toss.build_payload(_product(), _settings(), NOTICE)["productMandatoryNotice"]["mandatoryNoticeItems"]}
    assert items[901] == "충주 사과 (특)" and items[904] == "국산(충북 충주)" and items[903] == "충주농원"
    assert items[908] == "냉장 보관" and items[910] == "1588-0000" and items[902] == "3kg, 5kg"
    assert items[999] == "상세페이지 참고"


def test_delivery_group_is_omitted_when_not_set_and_tags_follow_the_rule():
    settings = _settings()
    settings["toss"]["delivery_location_id"] = ""
    assert "deliveryLocationId" not in toss.build_payload(_product(), settings, NOTICE)["deliveryPolicy"]
    tags = toss.search_tags(_product(name="충주 사과 특별한가격대상품 (특)"))
    assert tags and all(1 <= len(t) <= 10 and t.isalnum() for t in tags)


def test_parse_order_uses_real_phone_and_management_codes():
    row = {
        "orderProductId": 555, "orderId": 9, "productManagementCode": "agri-7", "productItemManagementCode": "agri-12",
        "productName": "충주 사과", "optionName": "5kg", "quantity": 2, "price": 36000, "ordererName": "김영희",
        "receiverName": "김철수", "receiverPhone": "0504-1", "receiverRealPhone": "010-9", "address": "서울", "detailAddress": "101동",
        "zipCode": "12345", "shippingNote": "문 앞", "orderProductStatus": "PAID",
    }
    order = _parse_order(row)
    assert order.order_product_id == "555" and order.item_code == "agri-12" and order.receiver_phone == "010-9"
    assert order.address == "서울 101동" and order.price == 36000 and order.status == "PAID"


def test_courier_names():
    assert toss_orders.courier_name("cj 대한통운") == "CJ대한통운"
    assert toss_orders.courier_name("우체국") == "우체국택배"
    assert toss_orders.courier_name("이상한택배") is None


async def test_mock_client_flow_and_claims():
    client = MockTossClient()
    order = client.add_order(product_code="agri-1", item_code="agri-1")
    assert order.order_product_id.startswith("MOCK-")
    claim = client.add_cancel_request(order)
    assert order.status == "CLAIM_REQUESTED_CANCEL" and [c.claim_id for c in await client.fetch_claims("CANCEL")] == [claim.claim_id]
    await client.reject_cancel(claim.claim_id, "CJ대한통운", "123")
    assert order.status == "DELIVERING" and order.tracking_no == "123"


# ---------------------------------------------------------------- 실제 클라이언트가 보내는 모양


def _real(handler):
    return TossClient("AK", "SK", transport=httpx.MockTransport(handler), base_url="https://fep.test", token_url="https://oauth.test/token")


def _ok(success=None):
    return httpx.Response(200, json={"resultType": "SUCCESS", "success": success if success is not None else {}})


def _auth(request):
    return httpx.Response(200, json={"access_token": "TOKEN", "expires_in": 31535999, "token_type": "Bearer"})


async def test_token_is_requested_once_with_form_body_and_reused():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth.test":
            calls.append(request.content.decode())
            return _auth(request)
        assert request.headers["authorization"] == "Bearer TOKEN"
        return _ok({"items": [{"id": 1, "name": "식품", "level": 1, "isLeaf": False}]})

    client = _real(handler)
    await client.categories()
    await client.categories(1)
    assert len(calls) == 1
    for part in ("grant_type=client_credentials", "client_id=AK", "client_secret=SK", "scope=toss-shopping-fep%3Awrite"):
        assert part in calls[0]


async def test_login_failure_and_fail_result_are_readable():
    with pytest.raises(TossError) as exc:
        await _real(lambda r: httpx.Response(401, json={"error": "invalid_client"})).categories()
    assert "토큰" in str(exc.value) and "IP" in str(exc.value)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth.test":
            return _auth(request)
        return httpx.Response(200, json={"resultType": "FAIL", "error": {"errorCode": "InvalidRequest", "reason": "잘못된 타입의 파라미터"}})

    with pytest.raises(TossError) as exc:
        await _real(handler).register_product({})
    assert "InvalidRequest" in str(exc.value) and "잘못된 타입" in str(exc.value)


async def test_register_product_posts_v3_and_returns_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth.test":
            return _auth(request)
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return _ok({"id": 123})

    assert await _real(handler).register_product({"productName": "x"}) == 123
    assert seen["path"] == "/api/v3/shopping-fep/products/v3" and seen["body"] == {"productName": "x"}


async def test_fetch_orders_follows_cursor():
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth.test":
            return _auth(request)
        params = dict(request.url.params)
        pages.append(params)
        if "nextCursor" not in params:
            return _ok({"results": [{"orderProductId": 1}], "nextCursor": "C2"})
        return _ok({"results": [{"orderProductId": 2}], "nextCursor": None})

    orders = await _real(handler).fetch_orders(date(2026, 11, 1), date(2026, 11, 6))
    assert [o.order_product_id for o in orders] == ["1", "2"]
    assert pages[0]["startDate"] == "2026-11-01" and pages[1]["nextCursor"] == "C2" and pages[0]["limit"] == "50"


async def test_dispatch_delay_and_claim_calls():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth.test":
            return _auth(request)
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path.endswith("/orders/products/status"):
            return _ok({"totalCount": 2, "failedCount": 1, "failedReasons": ["이미 지연 처리됨"]})
        if request.url.path.endswith("/claims"):
            return _ok({"items": [{"id": "CL1", "type": "CANCEL", "status": "REQUESTED", "requestReason": "변심", "order": {"orderProductId": 555}}], "hasNext": False})
        return _ok()

    client = _real(handler)
    await client.dispatch("555", "CJ대한통운", "1234-56")
    due = datetime(2026, 11, 9, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    outcomes = await client.delay_shipping(["1", "2"], due, "READY_PRODUCT")
    [claim] = await client.fetch_claims("CANCEL")
    await client.approve_cancel("CL1")
    await client.reject_cancel("CL1", "CJ대한통운", "999")
    await client.approve_return("CL2")

    assert seen[0] == ("PUT", "/api/v3/shopping-fep/orders/products/delivery", {"orderProductId": 555, "deliveryCompany": "CJ대한통운", "trackingNumber": "1234-56"})
    assert seen[1][2] == {"orderProductIds": [1, 2], "status": "DELAY_SHIPPING", "shippingDeadlineAt": "2026-11-09T12:00:00", "delayReasonType": "READY_PRODUCT"}
    assert [o.ok for o in outcomes] == [True, True] and "이미 지연" in outcomes[0].message  # 일부 실패는 성공한 건을 유지하고 사유를 알린다
    assert claim.claim_id == "CL1" and claim.order_product_id == "555"
    assert seen[3][1].endswith("/claims/CL1/cancel/approval") and seen[4][1].endswith("/claims/CL1/cancel/rejection")
    assert seen[4][2] == {"deliveryCompany": "CJ대한통운", "shippingTrackingNumber": "999"}
    assert seen[5][1].endswith("/claims/CL2/return/approval")


def test_real_client_requires_keys():
    with pytest.raises(ValueError):
        TossClient("", "")
