import asyncio

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import SessionLocalSync
from app.integrations.markets.coupang import CoupangWingClient
from app.models.customer_order import CustomerOrder
from app.models.enums import MarketType
from app.models.master_product import MasterProduct
from app.services.order_processor import process_new_order


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
                )
                session.add(order)
                created_order_ids.append(market_order_id)

        session.commit()
        return {"created_order_ids": created_order_ids, "skipped_unknown_sku": skipped_unknown_sku}


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
