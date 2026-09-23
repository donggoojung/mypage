from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocalSync, get_db
from app.models.customer_order import CustomerOrder
from app.models.return_request import ReturnRequest
from app.services import return_service

router = APIRouter(prefix="/api/returns", tags=["returns"])


@router.get("")
async def list_returns(session: AsyncSession = Depends(get_db)) -> list[dict]:
    """대시보드 "반품/취소 관리" 표에 쓰인다 — 반품 접수 건 + 연결된 주문 정보를 합쳐서 반환."""
    returns = (
        (await session.execute(select(ReturnRequest).order_by(ReturnRequest.return_id.desc()))).scalars().all()
    )

    output = []
    for r in returns:
        order = (
            await session.execute(select(CustomerOrder).where(CustomerOrder.order_id == r.order_id))
        ).scalar_one_or_none()
        output.append(
            {
                "return_id": r.return_id,
                "order_id": r.order_id,
                "market_order_id": order.market_order_id if order else None,
                "market_claim_id": r.market_claim_id,
                "claim_reason": r.claim_reason,
                "inspection_status": r.inspection_status.value,
                "source_refund_amount": float(r.source_refund_amount) if r.source_refund_amount is not None else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return output


@router.post("/{return_id}/inspection-passed")
async def inspection_passed(return_id: int) -> dict:
    """PRD 7.1: 거점 창고 검수 통과 — 소싱처 반품 접수 안내 텔레그램을 발송한다."""
    with SessionLocalSync() as session:
        try:
            return_request = await return_service.mark_inspection_passed(session, return_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"return_id": return_request.return_id, "inspection_status": return_request.inspection_status.value}


@router.post("/{return_id}/inspection-failed")
async def inspection_failed(return_id: int) -> dict:
    """PRD 7.3: 검수 불합격(훼손/시착흔적 등) — 분쟁 조사 상태로 전환한다."""
    with SessionLocalSync() as session:
        try:
            return_request = return_service.mark_inspection_failed(session, return_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"return_id": return_request.return_id, "inspection_status": return_request.inspection_status.value}


@router.post("/{return_id}/source-return-completed")
async def source_return_completed(return_id: int, source_refund_amount: float | None = None) -> dict:
    """관리자가 ABC마트 반품 접수/환불 확인을 마친 뒤 수동으로 완료 처리한다 (PRD 7.2 비용 정산)."""
    with SessionLocalSync() as session:
        try:
            return_request = return_service.mark_source_return_completed(
                session, return_id, source_refund_amount=source_refund_amount
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"return_id": return_request.return_id, "inspection_status": return_request.inspection_status.value}
