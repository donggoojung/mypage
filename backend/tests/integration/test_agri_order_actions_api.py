"""발송 지연 알리기(2-10)와 발송 전 취소 요청 처리(2-9): 롯데온·스마트스토어 연습 모드."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.markets import lotteon, naver_commerce
from app.main import app
from app.services import agri_order_actions as actions


@pytest.fixture(autouse=True)
def _fresh_mock_clients(monkeypatch):
    for module in (lotteon, naver_commerce):
        monkeypatch.setattr(module, "_mock_client", None)
        monkeypatch.setattr(module, "_real_client", None)


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _day(offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset)).astimezone(actions.KST).date().isoformat()


async def _product(client):
    supplier = (await client.post("/api/agri/suppliers", json={"name": "행운프레시"})).json()
    res = await client.post(
        "/api/agri/products",
        json={
            "supplier_id": supplier["supplier_id"], "name": "충주 사과", "category": "fruit",
            "origin": "국산(충북 충주)", "producer": "충주농원", "image_url": "https://example.com/a.jpg",
            "options": [{"label": "3kg", "supply_price": 12000}],
        },
    )
    return res.json()


async def _lotteon_order(client, product):
    key = (await client.post("/api/agri/lotteon/orders/practice", json={"option_id": product["options"][0]["option_id"]})).json()["product_order_id"]
    await client.post("/api/agri/lotteon/orders/sync", json={"hours": 24})
    order = next(o for o in (await client.get("/api/agri/orders")).json() if o["market_order_no"] == key)
    return key, order


async def _smartstore_order(client, product):
    no = (await client.post("/api/agri/smartstore/orders/practice", json={"option_id": product["options"][0]["option_id"]})).json()["product_order_id"]
    await client.post("/api/agri/smartstore/orders/sync", json={"hours": 24})
    return next(o for o in (await client.get("/api/agri/orders")).json() if o["market_order_no"] == no)


async def _orders(client):
    return {o["order_id"]: o for o in (await client.get("/api/agri/orders")).json()}


# ---------------------------------------------------------------- 2-10 발송 지연


async def test_lotteon_delay_is_sent_to_market_and_recorded_without_fake_tracking(client):
    product = await _product(client)
    key, order = await _lotteon_order(client, product)
    res = await client.post("/api/agri/orders/delay", json={"order_ids": [order["order_id"]], "due_date": _day(3), "reason": "stock"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert [r["order_id"] for r in body["done"]] == [order["order_id"]] and body["failed"] == []

    sent = lotteon.get_lotteon_client().delays
    assert len(sent) == 1 and sent[0].reason_code == "STK_SHTG" and f"{sent[0].od_no}-{sent[0].od_seq}-{sent[0].proc_seq}" == key
    saved = (await _orders(client))[order["order_id"]]
    assert saved["delayed_until"] and saved["delay_reason"] == "stock" and saved["tracking_no"] is None
    assert saved["status"] == "new" and "발송 지연" in saved["memo"]


async def test_customer_requested_delay_uses_customer_reason(client):
    product = await _product(client)
    _, order = await _lotteon_order(client, product)
    await client.post("/api/agri/orders/delay", json={"order_ids": [order["order_id"]], "due_date": _day(2), "reason": "customer"})
    assert lotteon.get_lotteon_client().delays[0].reason_code == "CUST_REQ"


async def test_smartstore_delay_is_manual_and_recorded_locally(client):
    product = await _product(client)
    order = await _smartstore_order(client, product)
    res = (await client.post("/api/agri/orders/delay", json={"order_ids": [order["order_id"]], "due_date": _day(2)})).json()
    assert [r["order_id"] for r in res["manual"]] == [order["order_id"]] and res["done"] == []
    saved = (await _orders(client))[order["order_id"]]
    assert saved["delayed_until"] and "직접" in saved["memo"]


async def test_delay_rejects_past_date_and_skips_shipped_orders_and_cancel_requests(client):
    product = await _product(client)
    key, order = await _lotteon_order(client, product)
    bad = await client.post("/api/agri/orders/delay", json={"order_ids": [order["order_id"]], "due_date": _day(-1)})
    assert bad.status_code == 400 and "오늘보다 앞" in bad.json()["detail"]
    assert (await client.post("/api/agri/orders/delay", json={"order_ids": [order["order_id"]], "due_date": "내일"})).status_code == 400

    await client.put(f"/api/agri/orders/{order['order_id']}/status", json={"status": "shipped"})
    skipped = (await client.post("/api/agri/orders/delay", json={"order_ids": [order["order_id"]], "due_date": _day(2)})).json()
    assert skipped["done"] == [] and "이미 발송" in skipped["skipped"][0]["reason"]
    assert lotteon.get_lotteon_client().delays == []


async def test_real_order_is_not_delayed_in_practice_mode(client):
    product = await _product(client)
    order = (await client.post("/api/agri/orders", json={
        "market": "lotteon", "market_order_no": "2026110512345-1-1", "option_id": product["options"][0]["option_id"],
        "receiver_name": "홍길동", "receiver_phone": "010-1", "address": "서울", "sale_amount": 20000,
    })).json()
    res = (await client.post("/api/agri/orders/delay", json={"order_ids": [order["order_id"]], "due_date": _day(2)})).json()
    assert res["done"] == [] and "연습 모드" in res["skipped"][0]["reason"]


def _fake_order(**overrides):
    data = dict(
        status="ordered", claim_status=None, market="lotteon", delayed_until=None,
        ordered_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def test_overdue_marking():
    settings = {"lotteon": {"send_days": 2}}
    assert actions.is_overdue(_fake_order(), settings)
    assert not actions.is_overdue(_fake_order(ordered_at=datetime.now(timezone.utc) - timedelta(hours=20)), settings)
    assert not actions.is_overdue(_fake_order(status="shipped"), settings)
    assert not actions.is_overdue(_fake_order(claim_status="requested"), settings)
    assert not actions.is_overdue(_fake_order(delayed_until=datetime.now(timezone.utc) + timedelta(days=1)), settings)
    assert actions.is_overdue(_fake_order(delayed_until=datetime.now(timezone.utc) - timedelta(days=1)), settings)  # 알린 날짜도 지남
    assert actions.is_overdue(_fake_order(market="smartstore", ordered_at=datetime.now(timezone.utc) - timedelta(days=4)), settings)
    assert not actions.is_overdue(_fake_order(market="smartstore", ordered_at=datetime.now(timezone.utc) - timedelta(days=2)), settings)


async def test_order_list_flags_overdue_orders(client, db_session):
    from app.models.agri import AgriOrder

    product = await _product(client)
    _, order = await _lotteon_order(client, product)
    assert (await _orders(client))[order["order_id"]]["overdue"] is False
    row = db_session.get(AgriOrder, order["order_id"])
    row.ordered_at = datetime.now(timezone.utc) - timedelta(days=6)
    db_session.commit()
    assert (await _orders(client))[order["order_id"]]["overdue"] is True


# ---------------------------------------------------------------- 2-9 발송 전 취소 처리


async def _cancel_requested(client, product):
    key, order = await _lotteon_order(client, product)
    await client.post("/api/agri/orders/purchase-sheet", json={"order_ids": [order["order_id"]]})  # 공급사에 발주
    await client.post(f"/api/agri/lotteon/orders/practice/{key}/claim", json={"kind": "cancel"})
    await client.post("/api/agri/lotteon/orders/sync", json={"hours": 24})
    return key, order["order_id"]


async def test_supplier_blocked_means_cancel_is_approved_with_note(client):
    product = await _product(client)
    _, oid = await _cancel_requested(client, product)
    res = await client.post(f"/api/agri/orders/{oid}/claim/resolve", json={"decision": "blocked"})
    assert res.status_code == 200, res.text
    assert res.json()["result"] == "approved" and res.json()["order"]["status"] == "cancelled"
    assert "공급사가 출고를 막은" in res.json()["order"]["memo"]
    assert len(lotteon.get_lotteon_client().approved) == 1


async def test_supplier_already_shipped_saves_tracking_and_rejects_cancel(client):
    product = await _product(client)
    _, oid = await _cancel_requested(client, product)
    missing = await client.post(f"/api/agri/orders/{oid}/claim/resolve", json={"decision": "shipped"})
    assert missing.status_code == 400 and "송장번호" in missing.json()["detail"]
    assert lotteon.get_lotteon_client().held == []

    res = await client.post(f"/api/agri/orders/{oid}/claim/resolve", json={"decision": "shipped", "courier": "CJ대한통운", "tracking_no": "1234-5678-9012"})
    assert res.status_code == 200, res.text
    order = res.json()["order"]
    assert res.json()["result"] == "held" and order["claim_status"] == "rejected"
    assert order["status"] == "invoiced" and order["tracking_no"] == "123456789012"
    assert lotteon.get_lotteon_client().held[0][1] == "901"  # 이미 발송

    sent = (await client.post("/api/agri/lotteon/orders/dispatch", json={"order_ids": [oid]})).json()
    assert len(sent["sent"]) == 1  # 취소 거부 뒤에는 송장을 보낼 수 있다


async def test_smartstore_shipped_decision_only_saves_tracking_and_points_to_center(client):
    product = await _product(client)
    order = await _smartstore_order(client, product)
    oid = order["order_id"]
    await client.post("/api/agri/orders/purchase-sheet", json={"order_ids": [oid]})
    mock = naver_commerce.get_smartstore_client()
    await client.post(f"/api/agri/smartstore/orders/practice/{order['market_order_no']}/claim", json={"kind": "cancel"})
    await client.post("/api/agri/smartstore/orders/sync", json={"hours": 24})
    res = (await client.post(f"/api/agri/orders/{oid}/claim/resolve", json={"decision": "shipped", "courier": "CJ대한통운", "tracking_no": "111222333"})).json()
    assert res["result"] == "manual" and "스마트스토어센터" in res["message"]
    assert res["order"]["tracking_no"] == "111222333" and res["order"]["claim_status"] == "requested"
    assert mock.approved == []


async def test_resolve_needs_a_cancel_request(client):
    product = await _product(client)
    _, order = await _lotteon_order(client, product)
    res = await client.post(f"/api/agri/orders/{order['order_id']}/claim/resolve", json={"decision": "blocked"})
    assert res.status_code == 400 and "취소 요청이 없습니다" in res.json()["detail"]
    assert (await client.post("/api/agri/orders/9999/claim/resolve", json={"decision": "blocked"})).status_code == 404
