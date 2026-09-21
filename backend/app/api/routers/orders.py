from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.customer_order import CustomerOrder
from app.models.master_product import MasterProduct
from app.models.order_fulfillment import OrderFulfillment

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("")
async def list_orders(session: AsyncSession = Depends(get_db)) -> list[dict]:
    """대시보드 "주문 현황" 표에 쓰인다 — 주문 + 상품명 + 소싱처 매입 결과를 합쳐서 반환."""
    orders = (await session.execute(select(CustomerOrder).order_by(CustomerOrder.order_id.desc()))).scalars().all()

    output = []
    for order in orders:
        product = (
            await session.execute(select(MasterProduct).where(MasterProduct.product_id == order.product_id))
        ).scalar_one_or_none()
        fulfillment = (
            await session.execute(select(OrderFulfillment).where(OrderFulfillment.order_id == order.order_id))
        ).scalar_one_or_none()

        output.append(
            {
                "order_id": order.order_id,
                "market_type": order.market_type.value,
                "market_order_id": order.market_order_id,
                "product_name": product.product_name if product else None,
                "ordered_size": order.ordered_size,
                "quantity": order.quantity,
                "recipient_name": order.recipient_name,
                "paid_amount": float(order.paid_amount),
                "status": order.status.value,
                "source_platform": fulfillment.source_platform.value if fulfillment else None,
                "cost_paid": float(fulfillment.cost_paid) if fulfillment and fulfillment.cost_paid is not None else None,
                "tracking_no": fulfillment.tracking_no if fulfillment else None,
            }
        )
    return output
