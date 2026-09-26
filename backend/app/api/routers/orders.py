import csv
import io
from decimal import Decimal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.customer_order import CustomerOrder
from app.models.master_product import MasterProduct
from app.models.order_fulfillment import OrderFulfillment
from app.services.margin_engine import PLATFORM_DEFAULT_FEE_RATES
from app.workers.tasks.order_tasks import approve_order_payment

router = APIRouter(prefix="/api/orders", tags=["orders"])


class ApprovePaymentResponse(BaseModel):
    task_id: str


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


@router.get("/export.csv")
async def export_orders_csv(session: AsyncSession = Depends(get_db)) -> StreamingResponse:
    """대시보드 "주문 현황"의 [엑셀 다운로드] 버튼 — 세무 신고(부가세/종합소득세)용 매입매출
    내역을 CSV 한 장으로 내려준다. 엑셀에서 그대로 열어 쓸 수 있도록 UTF-8 BOM을 붙인다.
    """
    orders = (await session.execute(select(CustomerOrder).order_by(CustomerOrder.order_id.desc()))).scalars().all()

    buffer = io.StringIO()
    buffer.write("﻿")  # 엑셀이 한글을 깨지지 않게 읽도록 하는 UTF-8 BOM.
    writer = csv.writer(buffer)
    writer.writerow(
        ["쿠팡 주문번호", "주문일", "상품명", "쿠팡 판매가", "쿠팡 수수료(추정)", "ABC마트 매입가", "순이익(추정)", "상태"]
    )

    for order in orders:
        product = (
            await session.execute(select(MasterProduct).where(MasterProduct.product_id == order.product_id))
        ).scalar_one_or_none()
        fulfillment = (
            await session.execute(select(OrderFulfillment).where(OrderFulfillment.order_id == order.order_id))
        ).scalar_one_or_none()

        paid_amount = Decimal(str(order.paid_amount))
        fee_rate = PLATFORM_DEFAULT_FEE_RATES.get(order.market_type, Decimal("0"))
        market_fee = (paid_amount * fee_rate).quantize(Decimal("1"))
        cost_paid = Decimal(str(fulfillment.cost_paid)) if fulfillment and fulfillment.cost_paid is not None else None
        net_profit = (paid_amount - market_fee - cost_paid) if cost_paid is not None else None

        writer.writerow(
            [
                order.market_order_id,
                order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else "",
                product.product_name if product else "",
                int(paid_amount),
                int(market_fee),
                int(cost_paid) if cost_paid is not None else "",
                int(net_profit) if net_profit is not None else "",
                order.status.value,
            ]
        )

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders.csv"},
    )


@router.post("/{order_id}/approve-payment", response_model=ApprovePaymentResponse)
def approve_payment(order_id: int) -> ApprovePaymentResponse:
    """대시보드의 [결제 승인] 버튼 — 즉시 응답하고 실제 RPA 결제는 Celery 워커가 백그라운드로 처리한다.

    진행 상태는 기존 /api/pipeline/status/{task_id} (Celery task_id 범용 조회)로 폴링하면 된다.
    """
    task = approve_order_payment.delay(order_id)
    return ApprovePaymentResponse(task_id=task.id)
