from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocalSync, get_db
from app.models.customer_inquiry import CustomerInquiry
from app.models.enums import InquiryStatus
from app.services import inquiry_service
from app.workers.tasks.inquiry_tasks import collect_inquiries

router = APIRouter(prefix="/api/inquiries", tags=["inquiries"])


@router.get("")
async def list_inquiries(session: AsyncSession = Depends(get_db)) -> list[dict]:
    """대시보드 "고객문의" 탭 — 미답변 문의가 위로 오도록 정렬해서 반환한다."""
    rows = (
        (await session.execute(select(CustomerInquiry).order_by(CustomerInquiry.inquiry_id.desc()))).scalars().all()
    )
    rows = sorted(rows, key=lambda r: r.status != InquiryStatus.PENDING)
    return [
        {
            "inquiry_id": r.inquiry_id,
            "inquiry_type": r.inquiry_type.value,
            "status": r.status.value,
            "product_label": r.product_label,
            "question": r.question,
            "inquired_at": r.inquired_at,
            "ai_draft_answer": r.ai_draft_answer,
            "final_answer": r.final_answer,
            "answered_at": r.answered_at.isoformat() if r.answered_at else None,
        }
        for r in rows
    ]


@router.post("/collect")
def trigger_collect() -> dict:
    """대시보드 [새 문의 확인] 버튼 — 10분 주기를 기다리지 않고 바로 수집한다."""
    task = collect_inquiries.delay()
    return {"task_id": task.id}


class AnswerRequest(BaseModel):
    answer: str


@router.post("/{inquiry_id}/answer")
async def answer_inquiry(inquiry_id: int, payload: AnswerRequest) -> dict:
    """대표님이 확인·수정한 답변을 쿠팡에 등록한다 (자동 등록은 하지 않는다)."""
    with SessionLocalSync() as session:
        try:
            inquiry = await inquiry_service.submit_answer(session, inquiry_id, payload.answer)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 쿠팡 API 오류 내용을 대시보드에 그대로 보여준다.
            raise HTTPException(status_code=502, detail=f"쿠팡 답변 등록 실패: {exc}") from exc
        return {"inquiry_id": inquiry.inquiry_id, "status": inquiry.status.value}
