"""스마트스토어 등록 흐름 (Mock 클라이언트): 점검 → 미리보기 → 등록 → 중복 방지 → 삭제 보호."""

import httpx
import pytest

from app.integrations.markets import naver_commerce
from app.main import app
from app.models.agri import AgriListing


@pytest.fixture(autouse=True)
def _fresh_mock_client(monkeypatch):
    monkeypatch.setattr(naver_commerce, "_mock_client", None)
    monkeypatch.setattr(naver_commerce, "_real_client", None)


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _supplier(client):
    res = await client.post("/api/agri/suppliers", json={"name": "행운프레시"})
    assert res.status_code == 200, res.text
    return res.json()["supplier_id"]


async def _product(client, supplier_id, **extra):
    body = {
        "supplier_id": supplier_id,
        "name": "충주 사과",
        "category": "fruit",
        "origin": "국산(충북 충주)",
        "producer": "충주농원",
        "image_url": "https://example.com/apple.jpg",
        "options": [{"label": "3kg", "supply_price": 12000}, {"label": "5kg", "supply_price": 18000}],
    }
    body.update(extra)
    res = await client.post("/api/agri/products", json=body)
    assert res.status_code == 200, res.text
    return res.json()


async def _configure(client, **smartstore):
    settings = (await client.get("/api/agri/settings")).json()
    settings["cs_phone"] = "1588-0000"
    settings["smartstore"]["leaf_category_ids"] = {"fruit": "50000100"}
    settings["smartstore"].update(smartstore)
    res = await client.put("/api/agri/settings", json=settings)
    assert res.status_code == 200, res.text
    return res.json()


async def test_preview_lists_problems_before_settings_are_filled(client):
    product = await _product(client, await _supplier(client))
    res = await client.get(f"/api/agri/products/{product['product_id']}/smartstore/preview")
    body = res.json()
    assert res.status_code == 200
    assert body["mode"] == "mock" and body["payload_verified"] is False
    assert body["payload"] is None
    assert any("카테고리 번호" in p for p in body["problems"])
    assert any("고객센터 전화번호" in p for p in body["problems"])


async def test_register_blocked_until_problems_fixed_then_ok_and_not_twice(client):
    product = await _product(client, await _supplier(client))
    pid = product["product_id"]

    blocked = await client.post(f"/api/agri/products/{pid}/smartstore/register")
    assert blocked.status_code == 400
    assert "고칠 것이 있습니다" in blocked.json()["detail"]
    assert naver_commerce.get_smartstore_client().registered == []

    await _configure(client)
    preview = (await client.get(f"/api/agri/products/{pid}/smartstore/preview")).json()
    assert preview["problems"] == []
    assert preview["payload"]["originProduct"]["leafCategoryId"] == "50000100"
    assert [p["label"] for p in preview["prices"]] == ["3kg", "5kg"]

    ok = await client.post(f"/api/agri/products/{pid}/smartstore/register")
    assert ok.status_code == 200, ok.text
    listing = ok.json()["listing"]
    assert ok.json()["mode"] == "mock" and listing["is_mock"] is True
    assert listing["market_product_no"].startswith("MOCK-")
    sent = naver_commerce.get_smartstore_client().registered
    assert len(sent) == 1
    assert sent[0]["originProduct"]["images"]["representativeImage"]["url"].startswith("https://shop-phinf.pstatic.net/")

    again = await client.post(f"/api/agri/products/{pid}/smartstore/register")
    assert again.status_code == 400 and "이미" in again.json()["detail"]
    assert len(naver_commerce.get_smartstore_client().registered) == 1

    listed = (await client.get("/api/agri/products")).json()
    assert listed[0]["listings"]["smartstore"]["market_product_no"] == listing["market_product_no"]
    # Mock 으로 올린 기록은 가짜라서 상품을 지우는 걸 막지 않는다 (실제 등록과 달리)
    assert (await client.delete(f"/api/agri/products/{pid}")).status_code == 200


async def test_real_listing_protects_product_from_delete(client, db_session):
    product = await _product(client, await _supplier(client))
    db_session.add(
        AgriListing(product_id=product["product_id"], market="smartstore", market_product_no="999", is_mock=False)
    )
    db_session.commit()
    res = await client.delete(f"/api/agri/products/{product['product_id']}")
    assert res.status_code == 400 and "스마트스토어" in res.json()["detail"]


async def test_settings_without_smartstore_keep_saved_values(client):
    await _configure(client, return_fee=4500)
    settings = (await client.get("/api/agri/settings")).json()
    del settings["smartstore"]
    saved = (await client.put("/api/agri/settings", json=settings)).json()
    assert saved["smartstore"]["return_fee"] == 4500
    assert saved["smartstore"]["leaf_category_ids"]["fruit"] == "50000100"


async def test_category_number_must_be_digits(client):
    settings = (await client.get("/api/agri/settings")).json()
    settings["smartstore"]["leaf_category_ids"] = {"fruit": "과일"}
    res = await client.put("/api/agri/settings", json=settings)
    assert res.status_code == 400


async def test_unknown_product_returns_404(client):
    assert (await client.get("/api/agri/products/9999/smartstore/preview")).status_code == 404
    assert (await client.post("/api/agri/products/9999/smartstore/register")).status_code == 404
