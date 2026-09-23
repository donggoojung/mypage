import asyncio

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import SessionLocalSync
from app.integrations.markets.coupang import CoupangWingClient
from app.models.customer_order import CustomerOrder
from app.models.enums import MarketType
from app.models.master_product import MasterProduct
from app.services.order_processor import process_new_order
from app.services.return_service import detect_cancellations_and_returns
from app.services.shipment_service import ShipmentNotReadyError, confirm_shipment_for_order


@celery_app.task(name="order_tasks.detect_new_orders")
def detect_new_orders(use_mock: bool | None = None) -> dict:
    """PRD 5.1: 쿠팡 결제 완료 주문을 폴링해, 아직 없는 주문만 customer_orders에 적재한다.

    실서비스에서는 5분 주기 스케줄러(Celery beat)로 이 태스크를 호출한다.
    """
    settings = get_settings()
    with SessionLocalSync() as session:
        client = CoupangWingClient(settings=settings, use_mock=use_mock)
        order_sheets = asyncio.run(client.fetch_paid_order_sheets(settings.coupang_vendor_id))

        created_order_ids: list[str] = []
        skipped_unknown_sku: list[str] = []

        for sheet in order_sheets:
            market_order_id = str(sheet["orderId"])
            already_exists = (
                session.query(CustomerOrder).filter_by(market_order_id=market_order_id).first() is not None
            )
            if already_exists:
                continue

            receiver = sheet["receiver"]
            for item in sheet["orderItems"]:
                style_code, _, size = item["externalVendorSku"].rpartition("-")
                product = session.query(MasterProduct).filter_by(style_code=style_code).first()
                if product is None:
                    skipped_unknown_sku.append(item["externalVendorSku"])
                    continue

                order = CustomerOrder(
                    market_type=MarketType.COUPANG,
                    market_order_id=market_order_id,
                    product_id=product.product_id,
                    ordered_size=size,
                    quantity=item.get("shippingCount", 1),
                    recipient_name=receiver["name"],
                    recipient_phone=receiver.get("safeNumber") or receiver.get("phone", ""),
                    shipping_addr=f"{receiver.get('addr1', '')} {receiver.get('addr2', '')}".strip(),
                    paid_amount=item.get("salesPrice", 0),
                    # PRD 6.1 발송처리 때 쿠팡 송장업로드 API에 그대로 넘겨야 하는 식별자.
                    market_shipment_box_id=str(item["shipmentBoxId"]) if item.get("shipmentBoxId") else None,
                    market_vendor_item_id=str(item["vendorItemId"]) if item.get("vendorItemId") else None,
                )
                session.add(order)
                created_order_ids.append(market_order_id)

        session.commit()
        return {"created_order_ids": created_order_ids, "skipped_unknown_sku": skipped_unknown_sku}


@celery_app.task(name="order_tasks.detect_cancellations_and_returns")
def detect_cancellations_and_returns_task(use_mock: bool | None = None) -> dict:
    """PRD 7.1: 쿠팡 취소/반품을 폴링해 즉시 상태를 반영하고 필요 시 관리자에게 알린다.

    신규 주문 감지와 동일하게 5분 주기 스케줄러(Celery beat)로 호출한다 — 대표님이
    "가장 치명적"이라 지적한 부분(매입 완료 후 취소 시 배송비/매입비 손실 위험)이라
    같은 긴급도로 다룬다.
    """
    with SessionLocalSync() as session:
        return asyncio.run(detect_cancellations_and_returns(session, use_mock=use_mock))


@celery_app.task(name="order_tasks.process_order")
def process_order(order_id: int, use_mock: bool | None = None) -> dict:
    """PRD 5.1~5.2: 주문 1건에 대해 최저가 소싱처를 판별하고 무인 발주를 완료한다."""
    with SessionLocalSync() as session:
        fulfillment = asyncio.run(process_new_order(session, order_id, use_mock=use_mock))
        return {
            "fulfillment_id": fulfillment.fulfillment_id,
            "order_id": fulfillment.order_id,
            "source_platform": fulfillment.source_platform.value,
            "source_order_id": fulfillment.source_order_id,
            "cost_paid": fulfillment.cost_paid,
        }


@celery_app.task(name="order_tasks.confirm_shipment")
def confirm_shipment(order_id: int, use_mock: bool | None = None) -> dict:
    """PRD 6.1: 매입 완료된 주문의 운송장을 조회해 쿠팡에 발송처리하고 SHIPPED로 전환한다.

    소싱처가 아직 발송 준비 중이면(운송장 미발급) 에러 없이 "아직 준비중"으로 표시하고
    끝낸다 — 다음 주기 폴링에서 다시 시도하면 된다(ShipmentNotReadyError는 정상적인
    "재시도 필요" 신호이지 실패가 아니다).
    """
    with SessionLocalSync() as session:
        try:
            fulfillment = asyncio.run(confirm_shipment_for_order(session, order_id, use_mock=use_mock))
        except ShipmentNotReadyError as exc:
            return {"order_id": order_id, "shipped": False, "reason": str(exc)}
        return {
            "order_id": order_id,
            "shipped": True,
            "courier_code": fulfillment.courier_code,
            "tracking_no": fulfillment.tracking_no,
        }
