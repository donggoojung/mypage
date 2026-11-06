"""토스쇼핑 연동 흐름 (연습 모드): 점검 → 등록 → 주문 가져오기 → 송장 → 발송 지연 → 취소 승인·거절."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.integrations.markets import toss
from app.main import app
from app.models.agri import AgriListing
from app.services import agri_order_actions as actions


@pytest.fixture(autouse=True)
def _fresh_mock_client(monkeypatch):
    monkeypatch.setattr(toss, "_mock_client", None)
    monkeypatch.setattr(toss, "_real_client", None)


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
            "supplier_id": supplier["supplier_id"], "name": "충주 사과", "category": "fruit", "origin": "국산(충북 충주)",
            "producer": "충주농원", "image_url": "https://example.com/a.jpg", "storage": "냉장 보관",
            "options": [{"label": "3kg", "supply_price": 12000}, {"label": "5kg", "supply_price": 18000}],
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


async def _configure(client):
    settings = (await client.get("/api/agri/settings")).json()
    settings["cs_phone"] = "1588-0000"
    box = settings["toss"]
    box.update(notice_category_code="AGRI_FOOD", exchange_refund_location_id="2001", delivery_location_id="1001")
    box["leaf_category_ids"]["fruit"] = "101"
    box["sales_option_keys"]["fruit"] = "중량"
    res = await client.put("/api/agri/settings", json=settings)
    assert res.status_code == 200, res.text


async def _practice(client, option_id):
    res = await client.post("/api/agri/toss/orders/practice", json={"option_id": option_id, "quantity": 1})
    assert res.status_code == 200, res.text
    return res.json()["product_order_id"]


async def _sync(client):
    res = await client.post("/api/agri/toss/orders/sync", json={"hours": 24})
    assert res.status_code == 200, res.text
    return res.json()


async def _orders(client):
    return {o["order_id"]: o for o in (await client.get("/api/agri/orders")).json()}


async def _order(client, product, option=0):
    no = await _practice(client, product["options"][option]["option_id"])
    await _sync(client)
    return next(o for o in (await _orders(client)).values() if o["market_order_no"] == no)


async def _tracking(client, order_id, courier="CJ대한통운"):
    await client.post("/api/agri/orders/tracking", json={"rows": [{"order_id": order_id, "courier": courier, "tracking_no": "123456789012"}]})


async def test_meta_connection_and_lookup(client):
    assert (await client.get("/api/agri/meta")).json()["toss_mode"] == "mock"
    conn = (await client.get("/api/agri/toss/connection")).json()
    assert conn["mode"] == "mock" and conn["category_count"] >= 1
    codes = (await client.get("/api/agri/toss/lookup", params={"kind": "notice-codes"})).json()["data"]
    assert codes[0]["categoryCode"]
    kids = (await client.get("/api/agri/toss/lookup", params={"kind": "categories", "id": "100"})).json()["data"]
    assert any(k["isLeaf"] for k in kids)
    assert (await client.get("/api/agri/toss/lookup", params={"kind": "nope"})).status_code == 400


async def test_settings_are_kept_and_validated(client):
    await _configure(client)
    settings = (await client.get("/api/agri/settings")).json()
    del settings["toss"]
    saved = (await client.put("/api/agri/settings", json=settings)).json()
    assert saved["toss"]["notice_category_code"] == "AGRI_FOOD" and saved["toss"]["leaf_category_ids"]["fruit"] == "101"
    settings = (await client.get("/api/agri/settings")).json()
    settings["toss"]["leaf_category_ids"]["fruit"] = "과일"
    assert (await client.put("/api/agri/settings", json=settings)).status_code == 400


async def test_register_blocked_until_settings_filled_then_ok_and_not_twice(client):
    product = await _product(client)
    pid = product["product_id"]
    blocked = await client.post(f"/api/agri/products/{pid}/toss/register")
    assert blocked.status_code == 400 and "고칠 것이 있습니다" in blocked.json()["detail"]
    assert toss.get_toss_client().registered == []

    await _configure(client)
    preview = (await client.get(f"/api/agri/products/{pid}/toss/preview")).json()
    assert preview["problems"] == [] and preview["mode"] == "mock" and preview["payload"]["leafCategoryId"] == 101
    ok = await client.post(f"/api/agri/products/{pid}/toss/register")
    assert ok.status_code == 200, ok.text
    assert ok.json()["listing"]["market_product_no"].startswith("88") and ok.json()["listing"]["is_mock"] is True
    notice = toss.get_toss_client().registered[0]["productMandatoryNotice"]["mandatoryNoticeItems"]
    assert len(notice) == 10 and {i["content"] for i in notice} >= {"충주 사과", "국산(충북 충주)"}
    assert (await client.post(f"/api/agri/products/{pid}/toss/register")).status_code == 400
    assert (await client.get("/api/agri/products")).json()[0]["listings"]["toss"]["market_product_no"]


async def test_real_toss_listing_protects_product_from_delete(client, db_session):
    product = await _product(client)
    db_session.add(AgriListing(product_id=product["product_id"], market="toss", market_product_no="1", is_mock=False))
    db_session.commit()
    res = await client.delete(f"/api/agri/products/{product['product_id']}")
    assert res.status_code == 400 and "토스쇼핑" in res.json()["detail"]


async def test_sync_brings_in_order_and_warns_about_instant_cancel(client):
    product = await _product(client)
    no = await _practice(client, product["options"][1]["option_id"])
    result = await _sync(client)
    assert result["created"] == 1 and any("결제완료" in w for w in result["warnings"])
    [order] = (await _orders(client)).values()
    assert order["market"] == "toss" and order["market_order_no"] == no and order["is_practice"] is True
    assert order["option_label"] == "5kg" and order["unit_cost"] == 18000
    assert (await _sync(client))["created"] == 0


async def test_dispatch_maps_courier_name_and_marks_shipped(client):
    product = await _product(client)
    order = await _order(client, product)
    early = (await client.post("/api/agri/toss/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert early["sent"] == [] and "송장" in early["skipped"][0]["reason"]
    await _tracking(client, order["order_id"], courier="cj 대한통운")
    sent = (await client.post("/api/agri/toss/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert len(sent["sent"]) == 1
    assert toss.get_toss_client().dispatched == [(order["market_order_no"], "CJ대한통운", "123456789012")]
    assert (await _orders(client))[order["order_id"]]["status"] == "shipped"


async def test_unknown_courier_and_failed_dispatch_are_not_marked_shipped(client):
    product = await _product(client)
    order = await _order(client, product)
    await _tracking(client, order["order_id"], courier="이상한택배")
    res = (await client.post("/api/agri/toss/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert res["sent"] == [] and "택배사" in res["skipped"][0]["reason"]
    await _tracking(client, order["order_id"])
    toss.get_toss_client().fail_dispatch_for.add(order["market_order_no"])
    res = (await client.post("/api/agri/toss/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert res["failed"][0]["reason"] == "연습용 실패" and (await _orders(client))[order["order_id"]]["status"] == "invoiced"


async def test_delay_only_once_and_within_one_to_thirty_two_days(client):
    product = await _product(client)
    order = await _order(client, product)
    oid = order["order_id"]
    far = (await client.post("/api/agri/orders/delay", json={"order_ids": [oid], "due_date": _day(40)})).json()
    assert far["done"] == [] and "1일 이상 32일" in far["skipped"][0]["reason"]
    today = (await client.post("/api/agri/orders/delay", json={"order_ids": [oid], "due_date": _day(0)})).json()
    assert today["done"] == [] and "1일 이상 32일" in today["skipped"][0]["reason"]

    ok = (await client.post("/api/agri/orders/delay", json={"order_ids": [oid], "due_date": _day(3)})).json()
    assert [r["order_id"] for r in ok["done"]] == [oid]
    ids, deadline, reason = toss.get_toss_client().delays[0]
    assert ids == [order["market_order_no"]] and reason == "READY_PRODUCT"
    assert (await _orders(client))[oid]["delayed_until"]
    again = (await client.post("/api/agri/orders/delay", json={"order_ids": [oid], "due_date": _day(4)})).json()
    assert again["done"] == [] and "한 번만" in again["skipped"][0]["reason"]


async def _cancel_requested(client, product, option=0):
    order = await _order(client, product, option)
    await client.post("/api/agri/orders/purchase-sheet", json={"order_ids": [order["order_id"]]})  # 공급사에 발주함
    await client.post(f"/api/agri/toss/orders/practice/{order['market_order_no']}/claim", json={"kind": "cancel"})
    await _sync(client)
    return order["order_id"], order["market_order_no"]


async def test_cancel_request_is_flagged_and_blocks_dispatch(client):
    product = await _product(client)
    oid, _ = await _cancel_requested(client, product)
    flagged = (await _orders(client))[oid]
    assert (flagged["claim_type"], flagged["claim_status"]) == ("cancel", "requested")
    await _tracking(client, oid)
    blocked = (await client.post("/api/agri/toss/orders/dispatch", json={"order_ids": [oid]})).json()
    assert blocked["sent"] == [] and "취소" in blocked["skipped"][0]["reason"]


async def test_cancel_approval_asks_confirmation_when_supplier_was_ordered(client):
    product = await _product(client)
    oid, _ = await _cancel_requested(client, product)
    asked = await client.post(f"/api/agri/orders/{oid}/toss/claim/approve", json={})
    assert asked.status_code == 409 and "공급사" in asked.json()["detail"] and toss.get_toss_client().approved == []
    done = await client.post(f"/api/agri/orders/{oid}/toss/claim/approve", json={"confirmed": True})
    assert done.status_code == 200, done.text
    assert done.json()["order"]["status"] == "cancelled" and len(toss.get_toss_client().approved) == 1


async def test_supplier_blocked_resolves_with_approval_and_note(client):
    product = await _product(client)
    oid, _ = await _cancel_requested(client, product)
    res = await client.post(f"/api/agri/orders/{oid}/claim/resolve", json={"decision": "blocked"})
    assert res.status_code == 200, res.text
    assert res.json()["result"] == "approved" and res.json()["order"]["status"] == "cancelled"


async def test_supplier_already_shipped_rejects_cancel_with_tracking(client):
    product = await _product(client)
    oid, no = await _cancel_requested(client, product)
    missing = await client.post(f"/api/agri/orders/{oid}/claim/resolve", json={"decision": "shipped"})
    assert missing.status_code == 400 and "송장번호" in missing.json()["detail"]
    res = await client.post(f"/api/agri/orders/{oid}/claim/resolve", json={"decision": "shipped", "courier": "CJ대한통운", "tracking_no": "1234-5678-9012"})
    assert res.status_code == 200, res.text
    order = res.json()["order"]
    assert res.json()["result"] == "held" and order["claim_status"] == "rejected" and order["status"] == "shipped"
    [(claim_id, company, tracking)] = toss.get_toss_client().rejected
    assert company == "CJ대한통운" and tracking == "123456789012"  # 하이픈은 지워서 보낸다
    assert toss.get_toss_client()._order(no).status == "DELIVERING"


async def test_market_side_cancel_before_preparation_is_reflected(client):
    product = await _product(client)
    order = await _order(client, product)
    mock = toss.get_toss_client()
    mock._order(order["market_order_no"]).status = "CANCELED_PAYMENT"  # 고객이 결제완료 상태에서 바로 취소 (알림 없음)
    result = await _sync(client)
    assert result["cancelled"] == 1
    assert (await _orders(client))[order["order_id"]]["status"] == "cancelled"


async def test_real_order_is_not_touched_in_practice_mode(client):
    product = await _product(client)
    order = (await client.post("/api/agri/orders", json={
        "market": "toss", "market_order_no": "123456789", "option_id": product["options"][0]["option_id"],
        "receiver_name": "홍길동", "receiver_phone": "010-1", "address": "서울", "sale_amount": 20000,
    })).json()
    await _tracking(client, order["order_id"])
    res = (await client.post("/api/agri/toss/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert res["sent"] == [] and "연습 모드" in res["skipped"][0]["reason"]
