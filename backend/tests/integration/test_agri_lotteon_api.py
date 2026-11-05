"""롯데온 연동 흐름 (연습 모드): 점검 → 등록 → 주문 가져오기 → 송장 → 취소 승인."""

import httpx
import pytest

from app.integrations.markets import lotteon
from app.main import app
from app.models.agri import AgriListing


@pytest.fixture(autouse=True)
def _fresh_mock_client(monkeypatch):
    monkeypatch.setattr(lotteon, "_mock_client", None)
    monkeypatch.setattr(lotteon, "_real_client", None)


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _product(client, **extra):
    supplier = (await client.post("/api/agri/suppliers", json={"name": "행운프레시"})).json()
    body = {
        "supplier_id": supplier["supplier_id"], "name": "충주 사과", "category": "fruit",
        "origin": "국산(충북 충주)", "producer": "충주농원", "image_url": "https://example.com/apple.jpg",
        "options": [{"label": "3kg", "supply_price": 12000}, {"label": "5kg", "supply_price": 18000}],
    }
    body.update(extra)
    res = await client.post("/api/agri/products", json=body)
    assert res.status_code == 200, res.text
    return res.json()


async def _configure(client):
    settings = (await client.get("/api/agri/settings")).json()
    settings["cs_phone"] = "1588-0000"
    box = settings["lotteon"]
    box.update(tr_no="LO100", owhp_no="PLO1", rtrp_no="PLO2", dv_cst_pol_no="DLD1", dv_rgspr_grp_cd="GN101")
    box["standard_category_ids"]["fruit"] = "BC63080300"
    box["display_category_ids"]["fruit"] = "FC11130203"
    res = await client.put("/api/agri/settings", json=settings)
    assert res.status_code == 200, res.text


async def _practice(client, option_id, qty=1):
    res = await client.post("/api/agri/lotteon/orders/practice", json={"option_id": option_id, "quantity": qty})
    assert res.status_code == 200, res.text
    return res.json()["product_order_id"]


async def _sync(client):
    res = await client.post("/api/agri/lotteon/orders/sync", json={"hours": 24})
    assert res.status_code == 200, res.text
    return res.json()


async def _orders(client):
    return (await client.get("/api/agri/orders")).json()


async def _tracking(client, order_id, courier="CJ대한통운"):
    res = await client.post("/api/agri/orders/tracking", json={"rows": [{"order_id": order_id, "courier": courier, "tracking_no": "123456789012"}]})
    assert res.status_code == 200


async def test_meta_connection_and_markets(client):
    meta = (await client.get("/api/agri/meta")).json()
    assert meta["lotteon_mode"] == "mock" and "lotteon" in meta["markets"]
    conn = (await client.get("/api/agri/lotteon/connection")).json()
    assert conn["mode"] == "mock" and conn["identity"]["trNo"]


async def test_settings_keep_lotteon_values_when_omitted_and_reject_bad_codes(client):
    await _configure(client)
    settings = (await client.get("/api/agri/settings")).json()
    del settings["lotteon"]
    saved = (await client.put("/api/agri/settings", json=settings)).json()
    assert saved["lotteon"]["owhp_no"] == "PLO1" and saved["lotteon"]["standard_category_ids"]["fruit"] == "BC63080300"
    settings = (await client.get("/api/agri/settings")).json()
    settings["lotteon"]["standard_category_ids"]["fruit"] = "과 일"
    assert (await client.put("/api/agri/settings", json=settings)).status_code == 400


async def test_register_is_blocked_until_settings_are_filled_then_ok_and_not_twice(client):
    product = await _product(client)
    pid = product["product_id"]
    blocked = await client.post(f"/api/agri/products/{pid}/lotteon/register")
    assert blocked.status_code == 400 and "고칠 것이 있습니다" in blocked.json()["detail"]
    assert lotteon.get_lotteon_client().registered == []

    await _configure(client)
    preview = (await client.get(f"/api/agri/products/{pid}/lotteon/preview")).json()
    assert preview["problems"] == [] and preview["payload"]["scatNo"] == "BC63080300" and preview["mode"] == "mock"

    ok = await client.post(f"/api/agri/products/{pid}/lotteon/register")
    assert ok.status_code == 200, ok.text
    assert ok.json()["listing"]["market_product_no"].startswith("LO") and ok.json()["listing"]["is_mock"] is True
    assert len(lotteon.get_lotteon_client().registered) == 1
    again = await client.post(f"/api/agri/products/{pid}/lotteon/register")
    assert again.status_code == 400 and "이미" in again.json()["detail"]

    listed = (await client.get("/api/agri/products")).json()[0]["listings"]
    assert listed["lotteon"]["market_product_no"] and listed["smartstore"] is None


async def test_real_lotteon_listing_protects_product_from_delete(client, db_session):
    product = await _product(client)
    db_session.add(AgriListing(product_id=product["product_id"], market="lotteon", market_product_no="LO1", is_mock=False))
    db_session.commit()
    res = await client.delete(f"/api/agri/products/{product['product_id']}")
    assert res.status_code == 400 and "롯데온" in res.json()["detail"]


async def test_sync_brings_in_order_and_tells_lotteon_it_was_received(client):
    product = await _product(client)
    key = await _practice(client, product["options"][1]["option_id"], qty=2)
    result = await _sync(client)
    assert result["created"] == 1 and result["mode"] == "mock" and result["warnings"] == []
    mock = lotteon.get_lotteon_client()
    assert mock.completed == [key]  # 가져온 주문은 롯데온에 '연동완료'를 알린다

    [order] = await _orders(client)
    assert order["market"] == "lotteon" and order["market_order_no"] == key and order["is_practice"] is True
    assert order["option_label"] == "5kg" and order["quantity"] == 2 and order["unit_cost"] == 18000 and order["status"] == "new"
    assert (await _sync(client))["created"] == 0  # 두 번 가져와도 중복되지 않는다
    assert len(await _orders(client)) == 1


async def test_unknown_product_order_is_skipped_and_not_completed(client):
    mock = lotteon.get_lotteon_client()
    mock.add_order(epd_no="other-1", receiver_name="홍길동", address="서울")
    result = await _sync(client)
    assert result["created"] == 0 and "우리 시스템" in result["skipped"][0]["reason"]
    assert mock.completed == []  # 못 가져온 주문은 연동완료를 보내지 않는다


async def test_dispatch_checks_sendable_then_marks_shipped(client):
    product = await _product(client)
    await _practice(client, product["options"][0]["option_id"])
    await _sync(client)
    [order] = await _orders(client)
    oid = order["order_id"]

    early = (await client.post("/api/agri/lotteon/orders/dispatch", json={"order_ids": [oid]})).json()
    assert early["sent"] == [] and "송장" in early["skipped"][0]["reason"]

    await _tracking(client, oid)
    sent = (await client.post("/api/agri/lotteon/orders/dispatch", json={"order_ids": [oid]})).json()
    assert len(sent["sent"]) == 1 and sent["mode"] == "mock"
    item = lotteon.get_lotteon_client().dispatched[0]
    assert item.courier_code == "0002" and item.tracking_no == "123456789012" and item.quantity == 1
    assert (await _orders(client))[0]["status"] == "shipped"
    again = (await client.post("/api/agri/lotteon/orders/dispatch", json={"order_ids": [oid]})).json()
    assert again["sent"] == []


async def test_unsendable_and_failed_orders_are_not_marked_shipped(client):
    product = await _product(client)
    key = await _practice(client, product["options"][0]["option_id"])
    await _sync(client)
    [order] = await _orders(client)
    await _tracking(client, order["order_id"])
    mock = lotteon.get_lotteon_client()

    mock.unsendable.add(key)
    res = (await client.post("/api/agri/lotteon/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert res["sent"] == [] and "불가능" in res["failed"][0]["reason"] and mock.dispatched == []

    mock.unsendable.clear()
    mock.fail_dispatch_for.add(key)
    res = (await client.post("/api/agri/lotteon/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert res["failed"][0]["reason"] == "연습용 실패" and (await _orders(client))[0]["status"] == "invoiced"


async def test_unknown_courier_is_not_sent(client):
    product = await _product(client)
    await _practice(client, product["options"][0]["option_id"])
    await _sync(client)
    [order] = await _orders(client)
    await _tracking(client, order["order_id"], courier="이상한택배")
    res = (await client.post("/api/agri/lotteon/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert res["sent"] == [] and "택배사" in res["skipped"][0]["reason"]


async def test_cancel_request_is_flagged_blocks_dispatch_and_can_be_approved(client):
    product = await _product(client)
    key = await _practice(client, product["options"][0]["option_id"])
    await _sync(client)
    [order] = await _orders(client)
    oid = order["order_id"]
    nothing = await client.post(f"/api/agri/orders/{oid}/lotteon/claim/approve", json={})
    assert nothing.status_code == 400 and "취소 요청이 없습니다" in nothing.json()["detail"]

    await client.post("/api/agri/orders/purchase-sheet", json={"order_ids": [oid]})  # 공급사에 발주함
    assert (await client.post(f"/api/agri/lotteon/orders/practice/{key}/claim", json={"kind": "cancel"})).status_code == 200
    changed = await _sync(client)
    assert changed["claims_updated"] == 1
    flagged = (await _orders(client))[0]
    assert (flagged["claim_type"], flagged["claim_status"]) == ("cancel", "requested")

    await _tracking(client, oid)
    blocked = (await client.post("/api/agri/lotteon/orders/dispatch", json={"order_ids": [oid]})).json()
    assert blocked["sent"] == [] and "취소" in blocked["skipped"][0]["reason"]

    asked = await client.post(f"/api/agri/orders/{oid}/lotteon/claim/approve", json={})
    assert asked.status_code == 409 and "공급사" in asked.json()["detail"]
    assert lotteon.get_lotteon_client().approved == []

    done = await client.post(f"/api/agri/orders/{oid}/lotteon/claim/approve", json={"confirmed": True})
    assert done.status_code == 200, done.text
    assert done.json()["order"]["status"] == "cancelled" and done.json()["order"]["claim_status"] == "done"
    assert len(lotteon.get_lotteon_client().approved) == 1


async def test_cancel_request_can_be_rejected_and_withdrawn_request_clears_flag(client):
    product = await _product(client)
    key = await _practice(client, product["options"][0]["option_id"])
    await _sync(client)
    [order] = await _orders(client)
    await client.post(f"/api/agri/lotteon/orders/practice/{key}/claim", json={"kind": "cancel"})
    await _sync(client)
    held = await client.post(f"/api/agri/orders/{order['order_id']}/lotteon/claim/hold")
    assert held.status_code == 200 and held.json()["order"]["claim_status"] == "rejected"
    assert lotteon.get_lotteon_client().held[0][1] == "902"

    other = await _practice(client, product["options"][1]["option_id"])
    await _sync(client)
    await client.post(f"/api/agri/lotteon/orders/practice/{other}/claim", json={"kind": "cancel"})
    await _sync(client)
    mock = lotteon.get_lotteon_client()
    mock.claims[-1].items[0].step = "22"  # 고객이 철회
    await _sync(client)
    cleared = next(o for o in await _orders(client) if o["market_order_no"] == other)
    assert cleared["claim_type"] is None and cleared["claim_status"] is None


async def test_return_request_comes_in_as_pickup_order_and_has_no_approval(client):
    product = await _product(client)
    key = await _practice(client, product["options"][0]["option_id"])
    await _sync(client)
    mock = lotteon.get_lotteon_client()
    original = mock.orders[0]
    mock.add_order(
        od_no=original.od_no, od_seq=original.od_seq, proc_seq=3, orgl_proc_seq=original.proc_seq, clm_no="CLM1",
        od_typ_cd="40", dv_rtrv_dvs_cd="RTRV", prgs_step="23", epd_no=original.epd_no, eitm_no=original.eitm_no,
    )
    result = await _sync(client)
    assert result["claims_updated"] == 1 and result["created"] == 0
    [order] = await _orders(client)
    assert (order["claim_type"], order["claim_status"]) == ("return", "requested")
    res = await client.post(f"/api/agri/orders/{order['order_id']}/lotteon/claim/approve", json={"confirmed": True})
    assert res.status_code == 400 and "승인 절차가 없고" in res.json()["detail"]


async def test_real_order_is_not_touched_in_practice_mode(client):
    product = await _product(client)
    order = (await client.post("/api/agri/orders", json={
        "market": "lotteon", "market_order_no": "2026110512345-1-1", "option_id": product["options"][0]["option_id"],
        "receiver_name": "홍길동", "receiver_phone": "010-1234-5678", "address": "서울", "sale_amount": 20000,
    })).json()
    await _tracking(client, order["order_id"])
    res = (await client.post("/api/agri/lotteon/orders/dispatch", json={"order_ids": [order["order_id"]]})).json()
    assert res["sent"] == [] and "연습 모드" in res["skipped"][0]["reason"]
    assert (await _orders(client))[0]["status"] == "invoiced"


async def test_practice_tools_refuse_unknown_things(client):
    assert (await client.post("/api/agri/lotteon/orders/practice", json={"option_id": 9999})).status_code == 400
    assert (await client.post("/api/agri/lotteon/orders/practice/NOPE-1-1/claim", json={"kind": "cancel"})).status_code == 404
    assert (await client.post("/api/agri/orders/9999/lotteon/claim/approve", json={})).status_code == 404
