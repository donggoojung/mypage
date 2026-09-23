import pytest

from app.models.customer_order import CustomerOrder
from app.models.enums import MarketType, OrderStatus, SourcePlatform
from app.models.master_product import MasterProduct
from app.models.order_fulfillment import OrderFulfillment
from app.models.source_mapping import SourceMapping
from app.services.sourcing_optimizer import NoAvailableSourceError
from app.workers.tasks import order_tasks

# Mock 쿠팡 신규주문 응답(coupang.py의 _mock_paid_order_sheets)은 항상
# orderId=700000001, externalVendorSku="CW2288-111-250" 1건을 반환한다.


@pytest.fixture
def matching_product(db_session):
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.commit()
    return product


def test_detect_new_orders_creates_customer_order(matching_product, db_session):
    result = order_tasks.detect_new_orders(use_mock=True)

    assert result["created_order_ids"] == ["700000001"]
    assert result["skipped_unknown_sku"] == []

    order = db_session.query(CustomerOrder).filter_by(market_order_id="700000001").first()
    assert order is not None
    assert order.product_id == matching_product.product_id
    assert order.ordered_size == "250"
    assert order.market_type == MarketType.COUPANG
    assert order.status == OrderStatus.RECEIVED
    assert order.recipient_name == "홍길동"


def test_detect_new_orders_is_idempotent(matching_product, db_session):
    order_tasks.detect_new_orders(use_mock=True)
    second_result = order_tasks.detect_new_orders(use_mock=True)

    assert second_result["created_order_ids"] == []
    assert db_session.query(CustomerOrder).count() == 1


def test_detect_new_orders_skips_order_without_matching_master_product(db_session):
    result = order_tasks.detect_new_orders(use_mock=True)

    assert result["created_order_ids"] == []
    assert result["skipped_unknown_sku"] == ["CW2288-111-250"]
    assert db_session.query(CustomerOrder).count() == 0


@pytest.fixture
def received_order_with_source(matching_product, db_session):
    db_session.add(
        SourceMapping(
            product_id=matching_product.product_id,
            source_platform=SourcePlatform.ABC_MART,
            source_url="https://mock.abcmart.example.com/products/CW2288-111",
            source_price=139000.0,
            size_stock_json={"250": {"stock": 5, "is_sold_out": False}},
        )
    )
    order = CustomerOrder(
        market_type=MarketType.COUPANG,
        market_order_id="700000001",
        product_id=matching_product.product_id,
        ordered_size="250",
        quantity=1,
        recipient_name="홍길동",
        recipient_phone="0501-1234-5678",
        shipping_addr="서울시 강남구 테헤란로 1 101호",
        paid_amount=192834,
    )
    db_session.add(order)
    db_session.commit()
    return order


def test_process_order_creates_fulfillment_and_marks_purchased(received_order_with_source, db_session):
    result = order_tasks.process_order(received_order_with_source.order_id, use_mock=True)

    assert result["source_platform"] == SourcePlatform.ABC_MART.value
    assert result["cost_paid"] == 139000.0
    assert result["source_order_id"].startswith("MOCK-CW2288-111-250-")

    db_session.refresh(received_order_with_source)
    assert received_order_with_source.status == OrderStatus.ORDER_PURCHASED

    fulfillment = db_session.query(OrderFulfillment).filter_by(order_id=received_order_with_source.order_id).first()
    assert fulfillment is not None
    assert fulfillment.source_platform == SourcePlatform.ABC_MART


def test_process_order_holds_when_no_source_available(matching_product, db_session):
    # SourceMapping을 하나도 만들지 않아 최저가 판별이 실패하는 상황을 재현한다.
    order = CustomerOrder(
        market_type=MarketType.COUPANG,
        market_order_id="700000002",
        product_id=matching_product.product_id,
        ordered_size="250",
        quantity=1,
        recipient_name="홍길동",
        recipient_phone="0501-1234-5678",
        shipping_addr="서울시 강남구 테헤란로 1",
        paid_amount=192834,
    )
    db_session.add(order)
    db_session.commit()

    with pytest.raises(NoAvailableSourceError):
        order_tasks.process_order(order.order_id, use_mock=True)

    db_session.refresh(order)
    assert order.status == OrderStatus.HOLD
