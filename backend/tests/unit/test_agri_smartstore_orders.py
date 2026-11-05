"""스마트스토어 주문 연동: 네이버 응답 읽기, 옵션 찾기, 택배사 코드, 실제 클라이언트의 요청 모양 (네이버에는 아무것도 보내지 않는다)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.markets.naver_commerce import (
    DispatchItem,
    MarketOrder,
    MockSmartstoreClient,
    NaverCommerceClient,
    NaverCommerceError,
    _parse_market_order,
)
from app.services import agri_smartstore_orders as so


def _detail(po_id="1001", **po):
    product_order = {
        "productOrderId": po_id,
        "productName": "충주 사과",
        "productOption": "3kg",
        "optionManageCode": "A3",
        "sellerProductCode": "agri-7",
        "quantity": 2,
        "totalPaymentAmount": 31000,
        "productOrderStatus": "PAYED",
        "shippingMemo": "문 앞에 놓아주세요",
        "shippingAddress": {
            "name": "김철수",
            "tel1": "010-1111-2222",
            "zipCode": "12345",
            "baseAddress": "서울시 강남구",
            "detailedAddress": "101동 202호",
        },
    }
    product_order.update(po)
    return {"order": {"orderId": "20260001", "ordererName": "김영희"}, "productOrder": product_order}


def test_parse_market_order_reads_detail_fields():
    mo = _parse_market_order(_detail())
    assert mo.product_order_id == "1001" and mo.order_id == "20260001"
    assert mo.product_code == "agri-7" and mo.option_code == "A3" and mo.option_text == "3kg"
    assert mo.quantity == 2 and mo.paid_amount == 31000
    assert mo.buyer_name == "김영희" and mo.receiver_name == "김철수" and mo.receiver_phone == "010-1111-2222"
    assert mo.address == "서울시 강남구 101동 202호" and mo.zipcode == "12345"
    assert mo.memo == "문 앞에 놓아주세요" and mo.status == "PAYED"


def test_parse_market_order_survives_missing_parts():
    mo = _parse_market_order({"productOrder": {"productOrderId": 5}})
    assert mo.product_order_id == "5" and mo.quantity == 1 and mo.address == "" and mo.order_id is None


@pytest.mark.parametrize(
    "name,expected",
    [("CJ대한통운", "CJGLS"), ("cj 대한통운", "CJGLS"), ("우체국택배", "EPOST"), ("롯데택배", "LOTTE"), ("CJGLS", "CJGLS"), ("모르는택배", None), (None, None), ("", None)],
)
def test_courier_code(name, expected):
    assert so.courier_code(name) == expected


@pytest.mark.parametrize(
    "ctype,cstatus,expected",
    [
        ("CANCEL", "CANCEL_REQUEST", ("cancel", "requested")),
        ("RETURN", "RETURN_REQUEST", ("return", "requested")),
        ("RETURN", "COLLECTING", ("return", "requested")),
        ("CANCEL", "CANCEL_DONE", ("cancel", "done")),
        ("RETURN", "RETURN_REJECT", ("return", "rejected")),
        ("EXCHANGE", "EXCHANGE_REQUEST", ("exchange", "requested")),
        (None, None, (None, None)),
    ],
)
def test_claim_mapping(ctype, cstatus, expected):
    assert so._claim_of(MarketOrder(product_order_id="1", claim_type=ctype, claim_status=cstatus)) == expected


class _FakeSession:
    def __init__(self, product):
        self._product = product

    def get(self, model, key):
        return self._product if self._product is not None and key == self._product.product_id else None


def _option(option_id, label, code=None):
    return SimpleNamespace(option_id=option_id, label=label, supplier_code=code)


def _product(options):
    return SimpleNamespace(product_id=7, options=options)


def test_find_option_by_manage_code_then_label_then_single():
    a3, a5 = _option(1, "3kg", "A3"), _option(2, "5kg", "A5")
    session = _FakeSession(_product([a3, a5]))
    assert so.find_option(session, MarketOrder("1", product_code="agri-7", option_code="A5"))[0] is a5
    assert so.find_option(session, MarketOrder("1", product_code="agri-7", option_text="용량: 3kg"))[0] is a3
    only = _FakeSession(_product([a3]))
    assert so.find_option(only, MarketOrder("1", product_code="agri-7"))[0] is a3


def test_find_option_gives_reasons_when_unknown():
    session = _FakeSession(_product([_option(1, "3kg"), _option(2, "5kg")]))
    option, reason = so.find_option(session, MarketOrder("1", product_code="agri-7", option_text="모름"))
    assert option is None and "옵션" in reason
    assert so.find_option(session, MarketOrder("1", product_code=None))[0] is None
    assert "지워졌" in so.find_option(session, MarketOrder("1", product_code="agri-99"))[1]


async def test_mock_client_records_and_updates_state():
    client = MockSmartstoreClient()
    order = client.add_order(product_code="agri-7", status="PAYED")
    assert order.product_order_id.startswith("MOCK-PO-")
    outcomes = await client.dispatch_orders([DispatchItem(order.product_order_id, "CJGLS", "123")])
    assert outcomes[0].ok and order.status == "DELIVERING"
    client.fail_dispatch_for.add("X")
    assert not (await client.dispatch_orders([DispatchItem("X", "CJGLS", "1")]))[0].ok


# ---------------------------------------------------------------- 실제 클라이언트가 보내는 모양


def _real(handler):
    import bcrypt

    return NaverCommerceClient("id", bcrypt.gensalt().decode(), transport=httpx.MockTransport(handler))


def _token(request):
    return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})


async def test_real_fetch_orders_follows_changed_list_then_queries_details():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/external/v1/oauth2/token":
            return _token(request)
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/last-changed-statuses"):
            assert request.url.params["lastChangedFrom"].endswith("+09:00")
            return httpx.Response(200, json={"data": {"lastChangeStatuses": [{"productOrderId": "1001"}, {"productOrderId": "1002"}]}})
        if request.url.path.endswith("/product-orders/query"):
            import json

            assert json.loads(request.content)["productOrderIds"] == ["1001", "1002"]
            return httpx.Response(200, json={"data": [_detail("1001"), _detail("1002")]})
        return httpx.Response(404)

    now = datetime.now(timezone.utc)
    orders = await _real(handler).fetch_orders(now - timedelta(hours=5), now)
    assert [o.product_order_id for o in orders] == ["1001", "1002"]
    assert [c[1].rsplit("/", 1)[-1] for c in calls] == ["last-changed-statuses", "query"]


async def test_real_fetch_orders_splits_long_period_and_pages():
    changed_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/external/v1/oauth2/token":
            return _token(request)
        if request.url.path.endswith("/last-changed-statuses"):
            changed_calls.append(dict(request.url.params))
            if "moreSequence" not in request.url.params and len(changed_calls) == 1:
                return httpx.Response(
                    200,
                    json={"data": {"lastChangeStatuses": [{"productOrderId": "1"}], "more": {"moreFrom": "2026-11-05T00:00:00.000+09:00", "moreSequence": "9"}}},
                )
            return httpx.Response(200, json={"data": {"lastChangeStatuses": []}})
        return httpx.Response(200, json={"data": [_detail("1")]})

    now = datetime.now(timezone.utc)
    orders = await _real(handler).fetch_orders(now - timedelta(hours=30), now)
    assert len(orders) == 1  # 같은 주문번호는 한 번만
    assert len(changed_calls) == 3  # 24시간 + 6시간 두 구간, 첫 구간에서 한 번 더 이어받기
    assert any(c.get("moreSequence") == "9" for c in changed_calls)


async def test_real_dispatch_sends_expected_body_and_reads_failures():
    import json

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/external/v1/oauth2/token":
            return _token(request)
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": {"successProductOrderIds": ["1"], "failProductOrderInfos": [{"productOrderId": "2", "code": "X", "message": "이미 발송됨"}]}},
        )

    outcomes = await _real(handler).dispatch_orders([DispatchItem("1", "CJGLS", "111"), DispatchItem("2", "CJGLS", "222")])
    assert seen["path"] == "/external/v1/pay-order/seller/product-orders/dispatch"
    first = seen["body"]["dispatchProductOrders"][0]
    assert first["productOrderId"] == "1" and first["deliveryMethod"] == "DELIVERY"
    assert first["deliveryCompanyCode"] == "CJGLS" and first["trackingNumber"] == "111" and "dispatchDate" in first
    assert [o.ok for o in outcomes] == [True, False]
    assert "이미 발송됨" in outcomes[1].message


async def test_real_claim_approval_calls_expected_paths_and_reports_errors():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/external/v1/oauth2/token":
            return _token(request)
        paths.append(request.url.path)
        if "return" in request.url.path:
            return httpx.Response(400, json={"message": "승인할 수 없는 상태"})
        return httpx.Response(200, json={})

    client = _real(handler)
    assert (await client.approve_cancel("55")).ok
    assert paths[-1] == "/external/v1/pay-order/seller/product-orders/55/claim/cancel/approve"
    with pytest.raises(NaverCommerceError) as exc:
        await client.approve_return("55")
    assert "승인할 수 없는 상태" in str(exc.value)
