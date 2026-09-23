"""PRD 5~6단계 전체 흐름 통합 테스트: 가상 쿠팡 주문 1건이

[주문 감지] → [ABC마트 최저가 매핑으로 무인 발주] → [운송장 조회] → [쿠팡 발송처리 + SHIPPED 전환]

까지 전부 Mock 모드로 매끄럽게 이어지는지 확인한다. 실API는 전혀 호출하지 않는다
(모든 단계가 use_mock=True) — 각 단계 자체의 실API 미검증 부분은 각 모듈에 이미
문서화되어 있고, 여기서는 "전체 파이프라인이 끊기지 않고 연결되는지"만 검증한다.

Mock 쿠팡 발주서 응답(coupang.py의 _mock_paid_order_sheets)은 항상 아래 1건을 돌려준다:
  orderId=700000001, externalVendorSku="CW2288-111-250",
  shipmentBoxId=900000001, vendorItemId=90000001
"""

import pytest

from app.models.customer_order import CustomerOrder
from app.models.enums import MarketType, OrderStatus, SourcePlatform
from app.models.master_product import MasterProduct
from app.models.order_fulfillment import OrderFulfillment
from app.models.source_mapping import SourceMapping
from app.services.shipment_service import ShipmentConfirmationError
from app.workers.tasks import order_tasks


def test_full_fulfillment_flow_from_order_detection_to_shipped(db_session):
    # --- 사전 준비: 주문이 매핑될 마스터 상품 + 매입 가능한 소싱처 ---
    product = MasterProduct(style_code="CW2288-111", brand_name="나이키", product_name="에어포스 1 '07 화이트")
    db_session.add(product)
    db_session.flush()
    db_session.add(
        SourceMapping(
            product_id=product.product_id,
            source_platform=SourcePlatform.ABC_MART,
            source_url="https://mock.abcmart.example.com/products/CW2288-111",
            source_price=139000.0,
            size_stock_json={"250": {"stock": 5, "is_sold_out": False}},
        )
    )
    db_session.commit()

    # --- 1단계: [가상 주문 감지] — 쿠팡 발주서 목록 조회 → customer_orders 적재 ---
    detect_result = order_tasks.detect_new_orders(use_mock=True)
    assert detect_result["created_order_ids"] == ["700000001"]

    order = db_session.query(CustomerOrder).filter_by(market_order_id="700000001").first()
    assert order is not None
    assert order.status == OrderStatus.RECEIVED
    assert order.recipient_name == "홍길동"
    # 발송처리 때 필요한 식별자도 발주서 조회 시점에 같이 저장돼야 한다.
    assert order.market_shipment_box_id == "900000001"
    assert order.market_vendor_item_id == "90000001"

    # --- 2단계: [ABC마트 최저가 매핑 + RPA 무인 발주] ---
    process_result = order_tasks.process_order(order.order_id, use_mock=True)
    assert process_result["source_platform"] == SourcePlatform.ABC_MART.value
    assert process_result["source_order_id"].startswith("MOCK-CW2288-111-250-")

    db_session.refresh(order)
    assert order.status == OrderStatus.ORDER_PURCHASED

    fulfillment = db_session.query(OrderFulfillment).filter_by(order_id=order.order_id).first()
    assert fulfillment is not None
    assert fulfillment.tracking_no is None  # 아직 발송처리 전.

    # --- 3단계: [운송장 조회] + [쿠팡 발송처리 API 호출 + SHIPPED 전환] ---
    shipment_result = order_tasks.confirm_shipment(order.order_id, use_mock=True)
    assert shipment_result["shipped"] is True
    assert shipment_result["courier_code"] == "CJGLS"
    assert shipment_result["tracking_no"]

    db_session.refresh(order)
    assert order.status == OrderStatus.SHIPPED

    db_session.refresh(fulfillment)
    assert fulfillment.courier_code == "CJGLS"
    assert fulfillment.tracking_no == shipment_result["tracking_no"]


def test_confirm_shipment_rejects_order_not_yet_purchased(db_session):
    """RECEIVED/SOURCING_IN_PROGRESS 상태 주문은 아직 매입 전이라 발송처리 대상이 아니어야 한다."""
    product = MasterProduct(style_code="CW2288-999", brand_name="나이키", product_name="테스트")
    db_session.add(product)
    db_session.flush()
    order = CustomerOrder(
        market_type=MarketType.COUPANG,
        market_order_id="700000099",
        product_id=product.product_id,
        ordered_size="250",
        quantity=1,
        recipient_name="홍길동",
        recipient_phone="0501-1234-5678",
        shipping_addr="서울시 강남구 테헤란로 1",
        paid_amount=192834,
        market_shipment_box_id="900000099",
        market_vendor_item_id="90000099",
    )
    db_session.add(order)
    db_session.commit()

    # 잘못된 상태에서의 발송처리 시도는 "나중에 재시도"가 아니라 진짜 오류라
    # (ShipmentNotReadyError가 아니라) 예외로 드러나야 한다 — 조용히 넘어가면 안 된다.
    with pytest.raises(ShipmentConfirmationError):
        order_tasks.confirm_shipment(order.order_id, use_mock=True)
