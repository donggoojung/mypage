"""신규 주문 1건을 소싱처 최저가 매입처에 무인 발주하는 전체 흐름 (PRD 5.1~5.2)."""

from sqlalchemy.orm import Session

from app.integrations.rpa.base import ShippingInfo
from app.integrations.rpa.factory import get_rpa_client
from app.models.customer_order import CustomerOrder
from app.models.enums import OrderStatus
from app.models.master_product import MasterProduct
from app.models.order_fulfillment import OrderFulfillment
from app.services.sourcing_optimizer import find_cheapest_source


class OrderProcessingError(RuntimeError):
    """주문 처리(소싱처 판별 또는 무인 발주) 중 복구 불가능한 오류."""


async def process_new_order(session: Session, order_id: int, use_mock: bool | None = None) -> OrderFulfillment:
    """RECEIVED 상태 주문 1건을 최저가 소싱처에서 무인 매입하고 ORDER_PURCHASED로 전환한다."""
    order = session.get(CustomerOrder, order_id)
    if order is None:
        raise ValueError(f"order_id={order_id} 인 customer_orders 행이 없습니다.")
    if order.status != OrderStatus.RECEIVED:
        raise OrderProcessingError(f"order_id={order_id} 는 이미 처리 중이거나 완료된 주문입니다 (status={order.status}).")

    product = session.get(MasterProduct, order.product_id)
    if product is None:
        raise ValueError(f"product_id={order.product_id} 인 master_products 행이 없습니다.")

    order.status = OrderStatus.SOURCING_IN_PROGRESS
    session.commit()

    try:
        quote = await find_cheapest_source(session, order.product_id, order.ordered_size, order.quantity)

        rpa_client = get_rpa_client(source_base_url=quote.source_url, use_mock=use_mock)
        shipping_info = ShippingInfo(
            recipient_name=order.recipient_name,
            recipient_phone=order.recipient_phone,
            shipping_addr=order.shipping_addr,
        )
        source_order_id = await rpa_client.purchase_order(quote.style_code, order.ordered_size, shipping_info)
    except Exception:
        order.status = OrderStatus.RECEIVED  # 실패 시 다음 폴링에서 재시도할 수 있게 원상 복구.
        session.commit()
        raise

    fulfillment = OrderFulfillment(
        order_id=order.order_id,
        source_platform=quote.source_platform,
        source_order_id=source_order_id,
        cost_paid=float(quote.expected_payment),
    )
    session.add(fulfillment)
    order.status = OrderStatus.ORDER_PURCHASED
    session.commit()

    return fulfillment
