"""스마트스토어 주문 연동 흐름 (Mock 클라이언트): 연습 주문 → 가져오기 → 송장 → 발송 처리 → 취소 승인."""

import httpx
import pytest

from app.integrations.markets import naver_commerce
from app.main import app


@pytest.fixture(autouse=True)
def _fresh_mock_client(monkeypatch):
    monkeypatch.setattr(naver_commerce, "_mock_client", None)
    monkeypatch.setattr(naver_commerce, "_real_client", None)


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _product(client):
    supplier = (await client.post("/api/agri/suppliers", json={"name": "행운프레시"})).json()
    res = await client.post(
        "/api/agri/products",
        json={
            "supplier_id": supplier["supplier_id"],
            "name": "충주 사과",
            "category": "fruit",
            "origin": "국산(충북 충주)",
            "producer": "충주농원",
            "options": [
                {"label": "3kg", "supply_price": 12000, "supplier_code": "A3"},
                {"label": "5kg", "supply_price": 18000, "supplier_code": "A5"},
            ],
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


async def _practice_order(client, option_id, quantity=1):
    res = await client.post("/api/agri/smartstore/orders/practice", json={"option_id": option_id, "quantity": quantity})
    assert res.status_code == 200, res.text
    return res.json()["product_order_id"]


async def _sync(client):
    res = await client.post("/api/agri/smartstore/orders/sync", json={"hours": 24})
    assert res.status_code == 200, res.text
    return res.json()


async def _orders(client):
    return (await client.get("/api/agri/orders")).json()


async def _give_tracking(client, order_id, courier="CJ대한통운", no="123456789012"):
    res = await client.post("/api/agri/orders/tracking", json={"rows": [{"order_id": order_id, "courier": courier, "tracking_no": no}]})
    assert res.status_code == 200, res.text


async def test_meta_tells_the_mode(client):
    assert (await client.get("/api/agri/meta")).json()["smartstore_mode"] == "mock"


async def test_sync_brings_in_practice_order_with_right_option_and_money(client):
    product = await _product(client)
    option_5kg = product["options"][1]
    po = await _practice_order(client, option_5kg["option_id"], quantity=2)

    result = await _sync(client)
    assert result["created"] == 1 and result["mode"] == "mock" and result["skipped"] == []

    [order] = await _orders(client)
    assert order["market"] == "smartstore" and order["market_order_no"] == po
    assert order["option_label"] == "5kg" and order["quantity"] == 2
    assert order["status"] == "new" and order["is_practice"] is True
    assert order["unit_cost"] == 18000 and order["sale_amount"] > 0

    again = await _sync(client)  # 두 번 가져와도 중복되지 않는다
    assert again["created"] == 0 and len(await _orders(client)) == 1


async def test_order_for_unknown_product_is_skipped_with_reason(client):
    mock = naver_commerce.get_smartstore_client()
    mock.add_order(product_code="other-shop-1", option_text="뭔가", receiver_name="홍길동", address="서울", status="PAYED")
    result = await _sync(client)
    assert result["created"] == 0 and len(result["skipped"]) == 1
    assert "우리 시스템" in result["skipped"][0]["reason"]


async def test_dispatch_needs_tracking_then_marks_shipped(client):
    product = await _product(client)
    await _practice_order(client, product["options"][0]["option_id"])
    await _sync(client)
    [order] = await _orders(client)

    early = (await client.post("/api/agri/smartstore/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert early["sent"] == [] and "송장" in early["skipped"][0]["reason"]

    await _give_tracking(client, order["order_id"])
    sent = (await client.post("/api/agri/smartstore/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert [s["order_id"] for s in sent["sent"]] == [order["order_id"]] and sent["mode"] == "mock"
    mock = naver_commerce.get_smartstore_client()
    assert mock.confirmed == [order["market_order_no"]]
    assert mock.dispatched[0].courier_code == "CJGLS" and mock.dispatched[0].tracking_no == "123456789012"
    assert (await _orders(client))[0]["status"] == "shipped"

    again = (await client.post("/api/agri/smartstore/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert again["sent"] == []  # 이미 보낸 주문은 다시 보내지 않는다


async def test_unknown_courier_is_not_sent(client):
    product = await _product(client)
    await _practice_order(client, product["options"][0]["option_id"])
    await _sync(client)
    [order] = await _orders(client)
    await _give_tracking(client, order["order_id"], courier="이상한택배")
    result = (await client.post("/api/agri/smartstore/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert result["sent"] == [] and "택배사" in result["skipped"][0]["reason"]
    assert naver_commerce.get_smartstore_client().dispatched == []


async def test_failed_dispatch_keeps_status_and_shows_reason(client):
    product = await _product(client)
    po = await _practice_order(client, product["options"][0]["option_id"])
    await _sync(client)
    [order] = await _orders(client)
    await _give_tracking(client, order["order_id"])
    naver_commerce.get_smartstore_client().fail_dispatch_for.add(po)
    result = (await client.post("/api/agri/smartstore/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert result["sent"] == [] and result["failed"][0]["reason"] == "연습용 실패"
    assert (await _orders(client))[0]["status"] == "invoiced"


async def test_order_with_cancel_request_is_not_dispatched_until_handled(client):
    product = await _product(client)
    po = await _practice_order(client, product["options"][0]["option_id"])
    await _sync(client)
    [order] = await _orders(client)
    await _give_tracking(client, order["order_id"])

    assert (await client.post(f"/api/agri/smartstore/orders/practice/{po}/claim", json={"kind": "cancel"})).status_code == 200
    changed = await _sync(client)
    assert changed["claims_updated"] == 1
    flagged = (await _orders(client))[0]
    assert flagged["claim_type"] == "cancel" and flagged["claim_status"] == "requested"

    result = (await client.post("/api/agri/smartstore/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert result["sent"] == [] and "취소" in result["skipped"][0]["reason"]


async def test_cancel_approval_asks_for_confirmation_when_supplier_was_ordered(client):
    product = await _product(client)
    po = await _practice_order(client, product["options"][0]["option_id"])
    await _sync(client)
    [order] = await _orders(client)
    oid = order["order_id"]

    nothing = await client.post(f"/api/agri/orders/{oid}/smartstore/claim/approve", json={})
    assert nothing.status_code == 400 and "승인할 취소" in nothing.json()["detail"]

    await client.post("/api/agri/orders/purchase-sheet", json={"order_ids": [oid]})  # 공급사에 발주함
    await client.post(f"/api/agri/smartstore/orders/practice/{po}/claim", json={"kind": "cancel"})
    await _sync(client)

    asked = await client.post(f"/api/agri/orders/{oid}/smartstore/claim/approve", json={})
    assert asked.status_code == 409 and "공급사" in asked.json()["detail"]
    assert naver_commerce.get_smartstore_client().approved == []

    done = await client.post(f"/api/agri/orders/{oid}/smartstore/claim/approve", json={"confirmed": True})
    assert done.status_code == 200, done.text
    assert done.json()["order"]["status"] == "cancelled" and done.json()["order"]["claim_status"] == "done"
    assert naver_commerce.get_smartstore_client().approved == [(po, "cancel")]


async def test_cancel_approval_without_supplier_order_needs_no_extra_confirmation(client):
    product = await _product(client)
    po = await _practice_order(client, product["options"][0]["option_id"])
    await _sync(client)
    await client.post(f"/api/agri/smartstore/orders/practice/{po}/claim", json={"kind": "cancel"})
    await _sync(client)
    [order] = await _orders(client)
    res = await client.post(f"/api/agri/orders/{order['order_id']}/smartstore/claim/approve", json={})
    assert res.status_code == 200 and res.json()["order"]["status"] == "cancelled"


async def test_market_side_cancel_and_manual_shipping_are_reflected(client):
    product = await _product(client)
    first = await _practice_order(client, product["options"][0]["option_id"])
    second = await _practice_order(client, product["options"][1]["option_id"])
    await _sync(client)

    mock = naver_commerce.get_smartstore_client()
    by_no = {o.product_order_id: o for o in mock.orders}
    by_no[first].status = "CANCELED"  # 고객이 발주 확인 전에 바로 취소
    by_no[second].status = "DELIVERING"  # 판매자센터에서 직접 발송 처리
    result = await _sync(client)
    assert result["cancelled"] == 1 and result["marked_shipped"] == 1

    orders = {o["market_order_no"]: o for o in await _orders(client)}
    assert orders[first]["status"] == "cancelled" and orders[first]["claim_status"] == "done"
    assert orders[second]["status"] == "shipped"


async def test_practice_tools_refuse_unknown_things(client):
    assert (await client.post("/api/agri/smartstore/orders/practice", json={"option_id": 9999})).status_code == 400
    missing = await client.post("/api/agri/smartstore/orders/practice/NOPE/claim", json={"kind": "cancel"})
    assert missing.status_code == 404
    assert (await client.post("/api/agri/orders/9999/smartstore/claim/approve", json={})).status_code == 404


async def test_real_order_is_not_touched_in_practice_mode(client):
    """연습 모드에서 진짜 주문 번호를 '발송 처리'하면 마켓에는 안 가고 우리만 발송됨으로 바뀌는 사고를 막는다."""
    product = await _product(client)
    res = await client.post(
        "/api/agri/orders",
        json={
            "market": "smartstore",
            "market_order_no": "2026110512345",
            "option_id": product["options"][0]["option_id"],
            "receiver_name": "홍길동",
            "receiver_phone": "010-1234-5678",
            "address": "서울",
            "sale_amount": 20000,
        },
    )
    order = res.json()
    await _give_tracking(client, order["order_id"])
    result = (await client.post("/api/agri/smartstore/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert result["sent"] == [] and "연습 모드" in result["skipped"][0]["reason"]
    assert (await _orders(client))[0]["status"] == "invoiced"
