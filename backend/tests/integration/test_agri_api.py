"""농산물 판매 관리 API: 공급사 → 상품 → 주문 → 발주서 → 송장 → 마켓 송장 입력 흐름."""

import httpx
import pytest

from app.main import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _supplier(client, name="행운프레시", shipping=0):
    res = await client.post("/api/agri/suppliers", json={"name": name, "default_shipping_fee": shipping})
    assert res.status_code == 200, res.text
    return res.json()


async def _product(client, supplier_id):
    res = await client.post(
        "/api/agri/products",
        json={
            "supplier_id": supplier_id,
            "name": "충주 사과",
            "category": "fruit",
            "origin": "국산(충북 충주)",
            "options": [
                {"label": "3kg", "supply_price": 12000},
                {"label": "5kg", "supply_price": 18000, "shipping_fee": 0},
            ],
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


def _order_body(option_id, order_no="2026110500001", amount=24000):
    return {
        "market": "smartstore",
        "market_order_no": order_no,
        "option_id": option_id,
        "quantity": 1,
        "receiver_name": "홍길동",
        "receiver_phone": "010-1111-2222",
        "zipcode": "12345",
        "address": "서울시 어딘가 1",
        "sale_amount": amount,
    }


async def test_full_flow(client):
    supplier = await _supplier(client)
    product = await _product(client, supplier["supplier_id"])
    option = product["options"][1]
    smart = next(p for p in option["prices"] if p["market"] == "smartstore")
    assert smart["profit"] >= 4000

    res = await client.post("/api/agri/orders", json=_order_body(option["option_id"], amount=smart["price"]))
    assert res.status_code == 200, res.text
    order = res.json()
    assert order["status"] == "new"
    assert order["supplier_name"] == "행운프레시"
    assert order["unit_cost"] == 18000

    dup = await client.post("/api/agri/orders", json=_order_body(option["option_id"], amount=smart["price"]))
    assert dup.status_code == 400

    sheet = (await client.post("/api/agri/orders/purchase-sheet", json={"order_ids": [order["order_id"]]})).json()
    assert list(sheet["groups"]) == ["행운프레시"]
    assert sheet["groups"]["행운프레시"][0]["status"] == "ordered"

    res = await client.post(
        "/api/agri/orders/tracking",
        json={"rows": [{"order_id": order["order_id"], "courier": "CJ대한통운", "tracking_no": "123456789012"}]},
    )
    assert res.json() == {"updated": 1, "skipped": []}

    res = await client.post("/api/agri/orders/market-shipped", json={"order_ids": [order["order_id"]]})
    assert res.json()["updated"] == 1
    orders = (await client.get("/api/agri/orders")).json()
    assert orders[0]["status"] == "shipped"
    assert orders[0]["tracking_no"] == "123456789012"

    summary = (await client.get("/api/agri/summary")).json()
    assert summary["month_orders"] == 1
    assert summary["month_profit"] == orders[0]["profit"]
    assert summary["status_counts"]["shipped"] == 1


async def test_cancelled_order_cannot_be_purchase_ordered(client):
    supplier = await _supplier(client)
    product = await _product(client, supplier["supplier_id"])
    order = (await client.post("/api/agri/orders", json=_order_body(product["options"][0]["option_id"]))).json()
    await client.put(f"/api/agri/orders/{order['order_id']}/status", json={"status": "cancelled"})
    res = await client.post("/api/agri/orders/purchase-sheet", json={"order_ids": [order["order_id"]]})
    assert res.status_code == 400


async def test_option_with_orders_cannot_be_removed(client):
    supplier = await _supplier(client)
    product = await _product(client, supplier["supplier_id"])
    first = product["options"][0]
    await client.post("/api/agri/orders", json=_order_body(first["option_id"]))

    body = {
        "supplier_id": supplier["supplier_id"],
        "name": "충주 사과",
        "options": [{"option_id": product["options"][1]["option_id"], "label": "5kg", "supply_price": 18000}],
    }
    res = await client.put(f"/api/agri/products/{product['product_id']}", json=body)
    assert res.status_code == 400
    assert "품절" in res.json()["detail"]

    # 옵션을 남겨두고 가격만 바꾸는 수정은 된다.
    body["options"].insert(0, {"option_id": first["option_id"], "label": "3kg", "supply_price": 13000})
    res = await client.put(f"/api/agri/products/{product['product_id']}", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["options"][0]["supply_price"] == 13000

    assert (await client.delete(f"/api/agri/products/{product['product_id']}")).status_code == 400
    assert (await client.delete(f"/api/agri/suppliers/{supplier['supplier_id']}")).status_code == 400


async def test_import_groups_rows_and_uses_supplier_shipping(client):
    supplier = await _supplier(client, name="캄므커머스", shipping=3500)
    rows = [
        {"product_name": "성주 참외", "option_label": "2kg", "supply_price": 15000},
        {"product_name": "성주 참외", "option_label": "3kg", "supply_price": 20000, "shipping_fee": 0},
        {"product_name": "해남 고구마", "category": "vegetable", "option_label": "5kg", "supply_price": 11000},
    ]
    res = await client.post("/api/agri/products/import", json={"supplier_id": supplier["supplier_id"], "rows": rows})
    assert res.json() == {"products_created": 2, "options_added": 3}

    # 같은 파일을 두 번 올려도 옵션이 중복으로 생기지 않는다.
    res = await client.post("/api/agri/products/import", json={"supplier_id": supplier["supplier_id"], "rows": rows})
    assert res.json() == {"products_created": 0, "options_added": 0}

    products = {p["name"]: p for p in (await client.get("/api/agri/products")).json()}
    melon = products["성주 참외"]
    assert [o["shipping_fee"] for o in melon["options"]] == [3500, 0]
    assert products["해남 고구마"]["category"] == "vegetable"


async def test_settings_change_prices_and_notice(client):
    settings = (await client.get("/api/agri/settings")).json()
    settings["min_profit"]["fruit"] = 5000
    settings["cs_phone"] = "010-9999-8888"
    res = await client.put("/api/agri/settings", json=settings)
    assert res.status_code == 200
    assert res.json()["min_profit"]["fruit"] == 5000

    preview = (await client.post("/api/agri/price-preview", json={"supply_price": 10000})).json()
    assert preview["min_profit"] == 5000
    assert all(r["profit"] >= 5000 for r in preview["rows"])

    supplier = await _supplier(client)
    product = await _product(client, supplier["supplier_id"])
    notice = (await client.get(f"/api/agri/products/{product['product_id']}/notice")).json()["text"]
    assert "010-9999-8888" in notice

    bad = dict(settings, fee_rates={"smartstore": 150})
    assert (await client.put("/api/agri/settings", json=bad)).status_code == 400
