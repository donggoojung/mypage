import asyncio

from app.core.celery_app import celery_app
from app.core.database import SessionLocalSync
from app.services.inquiry_service import collect_new_inquiries


@celery_app.task(name="inquiry_tasks.collect_inquiries")
def collect_inquiries(use_mock: bool | None = None) -> dict:
    """쿠팡 미답변 고객문의를 수집하고 AI 답변 초안을 붙인다 (10분 주기 beat + 대시보드 버튼)."""
    with SessionLocalSync() as session:
        return asyncio.run(collect_new_inquiries(session, use_mock=use_mock))
