import httpx
import pytest

from app.main import app
from app.models.enums import GenerationStatus, ListingStatus, MarketType, OrderStatus, SourcePlatform
from app.models.customer_order import CustomerOrder
from app.models.generated_asset import GeneratedAsset
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct
from app.models.order_fulfillment import OrderFulfillment

# TestClient(동기)는 FastAPI의 비동기 DB 세션(asyncpg)과 함께 쓰면 테스트마다 다른
# 이벤트루프를 만들어 "Event loop is closed" 오류가 난다 — pytest-asyncio가 관리하는
# 단일 이벤트루프 안에서 도는 httpx.AsyncClient + ASGITransport를 대신 쓴다.
@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_check(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_dashboard_served_at_root(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "대시보드" in response.text


@pytest.mark.asyncio
async def test_list_products_empty(client, db_session):
    response = await client.get("/api/products")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_products_returns_product_with_asset_and_listing(client, db_session):
    product = MasterProduct(style_code="API-TEST-001", brand_name="테스트브랜드", product_name="테스트 운동화")
    db_session.add(product)
    db_session.flush()

    db_session.add(
        GeneratedAsset(
            product_id=product.product_id,
            ai_thumbnail_url="https://mock-cdn.example.com/thumb.png",
            ai_detail_image_url="https://mock-cdn.example.com/detail.webp",
            generation_status=GenerationStatus.COMPLETED,
        )
    )
    db_session.add(
        MarketListing(
            product_id=product.product_id,
            market_type=MarketType.COUPANG,
            market_product_id="12345678",
            selling_price=139000,
            status=ListingStatus.DRAFT,
        )
    )
    db_session.commit()

    response = await client.get("/api/products")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["style_code"] == "API-TEST-001"
    assert body[0]["thumbnail_url"] == "https://mock-cdn.example.com/thumb.png"
    assert body[0]["selling_price"] == 139000.0
    assert body[0]["listing_status"] == "draft"


@pytest.mark.asyncio
async def test_list_orders_returns_order_with_product_and_fulfillment(client, db_session):
    product = MasterProduct(style_code="API-TEST-002", brand_name="테스트브랜드", product_name="테스트 운동화2")
    db_session.add(product)
    db_session.flush()

    order = CustomerOrder(
        market_type=MarketType.COUPANG,
        market_order_id="ORDER-API-TEST-1",
        product_id=product.product_id,
        ordered_size="270",
        quantity=1,
        recipient_name="홍길동",
        recipient_phone="0501-1234-5678",
        shipping_addr="서울시 강남구",
        paid_amount=150000,
        status=OrderStatus.ORDER_PURCHASED,
    )
    db_session.add(order)
    db_session.flush()

    db_session.add(
        OrderFulfillment(
            order_id=order.order_id,
            source_platform=SourcePlatform.ABC_MART,
            source_order_id="SRC-1",
            cost_paid=100000,
            tracking_no="123456789012",
        )
    )
    db_session.commit()

    response = await client.get("/api/orders")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["market_order_id"] == "ORDER-API-TEST-1"
    assert body[0]["product_name"] == "테스트 운동화2"
    assert body[0]["status"] == "order_purchased"
    assert body[0]["source_platform"] == "abc_mart"
    assert body[0]["tracking_no"] == "123456789012"


@pytest.mark.asyncio
async def test_run_pipeline_enqueues_celery_task(client, monkeypatch):
    """실제 크롤링/Celery 워커 없이, API가 태스크를 큐에 넣고 task_id를 돌려주는지만 검증한다."""

    class _FakeAsyncResult:
        id = "fake-task-id-123"

    def _fake_delay(url, options):
        assert url == "https://abcmart.a-rt.com/product?prdtNo=1"
        return _FakeAsyncResult()

    from app.api.routers import pipeline as pipeline_router

    monkeypatch.setattr(pipeline_router.run_pipeline_for_url_task, "delay", _fake_delay)

    response = await client.post("/api/pipeline/run", json={"url": "https://abcmart.a-rt.com/product?prdtNo=1"})
    assert response.status_code == 200
    assert response.json() == {"task_id": "fake-task-id-123"}


@pytest.mark.asyncio
async def test_refresh_all_pipeline_enqueues_celery_task(client, monkeypatch):
    class _FakeAsyncResult:
        id = "fake-refresh-task-id"

    def _fake_delay():
        return _FakeAsyncResult()

    from app.api.routers import pipeline as pipeline_router

    monkeypatch.setattr(pipeline_router.refresh_all_registered_products_task, "delay", _fake_delay)

    response = await client.post("/api/pipeline/refresh-all")
    assert response.status_code == 200
    assert response.json() == {"task_id": "fake-refresh-task-id"}


@pytest.mark.asyncio
async def test_run_pipeline_batch_enqueues_celery_task(client, monkeypatch):
    class _FakeAsyncResult:
        id = "fake-batch-task-id"

    def _fake_delay(urls, options):
        assert urls == ["https://abcmart.a-rt.com/product?prdtNo=1", "https://abcmart.a-rt.com/product?prdtNo=2"]
        return _FakeAsyncResult()

    from app.api.routers import pipeline as pipeline_router

    monkeypatch.setattr(pipeline_router.run_pipeline_for_urls_task, "delay", _fake_delay)

    response = await client.post(
        "/api/pipeline/run-batch",
        json={"urls": ["https://abcmart.a-rt.com/product?prdtNo=1", "https://abcmart.a-rt.com/product?prdtNo=2"]},
    )
    assert response.status_code == 200
    assert response.json() == {"task_id": "fake-batch-task-id"}


@pytest.mark.asyncio
async def test_run_pipeline_batch_rejects_empty_url_list(client):
    response = await client.post("/api/pipeline/run-batch", json={"urls": ["   ", ""]})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_discover_category_enqueues_celery_task(client, monkeypatch):
    class _FakeAsyncResult:
        id = "fake-discover-task-id"

    def _fake_delay(category_url, max_products, max_pages):
        assert category_url == "https://abcmart.a-rt.com/display/ranking/main"
        assert max_products == 20
        return _FakeAsyncResult()

    from app.api.routers import pipeline as pipeline_router

    monkeypatch.setattr(pipeline_router.discover_category_urls_task, "delay", _fake_delay)

    response = await client.post(
        "/api/pipeline/discover",
        json={"category_url": "https://abcmart.a-rt.com/display/ranking/main", "max_products": 20},
    )
    assert response.status_code == 200
    assert response.json() == {"task_id": "fake-discover-task-id"}


@pytest.mark.asyncio
async def test_export_orders_csv_returns_expected_columns(client, db_session):
    """대시보드 [엑셀 다운로드] 버튼 — 세무 신고용 매입매출 CSV에 필요한 컬럼이 다 있는지 확인."""
    product = MasterProduct(style_code="API-TEST-003", brand_name="테스트브랜드", product_name="테스트 운동화3")
    db_session.add(product)
    db_session.flush()

    order = CustomerOrder(
        market_type=MarketType.COUPANG,
        market_order_id="ORDER-API-TEST-2",
        product_id=product.product_id,
        ordered_size="270",
        quantity=1,
        recipient_name="홍길동",
        recipient_phone="0501-1234-5678",
        shipping_addr="서울시 강남구",
        paid_amount=150000,
        status=OrderStatus.ORDER_PURCHASED,
    )
    db_session.add(order)
    db_session.flush()

    db_session.add(
        OrderFulfillment(
            order_id=order.order_id,
            source_platform=SourcePlatform.ABC_MART,
            source_order_id="SRC-2",
            cost_paid=100000,
        )
    )
    db_session.commit()

    response = await client.get("/api/orders/export.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]

    body = response.text
    assert "쿠팡 주문번호" in body
    assert "순이익" in body
    assert "ORDER-API-TEST-2" in body
    assert "100000" in body  # ABC마트 매입가


@pytest.mark.asyncio
async def test_registration_status_reports_remaining_count(client, monkeypatch):
    """섹션 일괄 등록 탭의 [등록 가능 개수 확인] 버튼 — 남은 등록 가능 개수를 계산해 돌려준다."""

    async def _fake_fetch_registration_status(self):
        return {"vendorId": "A00123456", "restricted": False, "registeredCount": 8125, "permittedCount": 10000}

    from app.api.routers import pipeline as pipeline_router

    monkeypatch.setattr(
        pipeline_router.CoupangWingClient, "fetch_registration_status", _fake_fetch_registration_status
    )

    response = await client.get("/api/pipeline/registration-status")
    assert response.status_code == 200
    body = response.json()
    assert body["registered_count"] == 8125
    assert body["permitted_count"] == 10000
    assert body["remaining_count"] == 1875
    assert body["restricted"] is False


@pytest.mark.asyncio
async def test_registration_status_handles_unlimited_permitted_count(client, monkeypatch):
    async def _fake_fetch_registration_status(self):
        return {"vendorId": "A00123456", "restricted": False, "registeredCount": 4, "permittedCount": None}

    from app.api.routers import pipeline as pipeline_router

    monkeypatch.setattr(
        pipeline_router.CoupangWingClient, "fetch_registration_status", _fake_fetch_registration_status
    )

    response = await client.get("/api/pipeline/registration-status")
    assert response.status_code == 200
    body = response.json()
    assert body["permitted_count"] is None
    assert body["remaining_count"] is None


@pytest.mark.asyncio
async def test_list_products_includes_competitor_price_info(client, db_session):
    """등록된 상품 표 — 경쟁가 정보와 우리 가격 차이(%)가 같이 내려오는지 확인."""
    product = MasterProduct(style_code="API-TEST-004", brand_name="테스트브랜드", product_name="테스트 운동화4")
    db_session.add(product)
    db_session.flush()

    db_session.add(
        MarketListing(
            product_id=product.product_id,
            market_type=MarketType.COUPANG,
            market_product_id="87654321",
            selling_price=120000,
            status=ListingStatus.DRAFT,
            competitor_count=4,
            competitor_lowest_price=90000,
        )
    )
    db_session.commit()

    response = await client.get("/api/products")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["competitor_count"] == 4
    assert body[0]["competitor_lowest_price"] == 90000.0
    assert round(body[0]["competitor_gap_percent"]) == 33  # (120000-90000)/90000*100
