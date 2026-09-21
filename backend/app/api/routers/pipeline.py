from celery.result import AsyncResult
from fastapi import APIRouter
from pydantic import BaseModel

from app.core.celery_app import celery_app
from app.workers.tasks.pipeline_tasks import refresh_all_registered_products_task, run_pipeline_for_url_task

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class PipelineRunRequest(BaseModel):
    url: str
    request_approval: bool = False
    target_margin_rate: float = 0.30
    fixed_margin: float = 5000
    customer_shipping_charge: float = 3000
    source_shipping_cost: float = 0
    display_category_code: int = 56137
    category: str = "운동화"
    color_tone: str = "neutral"


class PipelineRunResponse(BaseModel):
    task_id: str


@router.post("/run", response_model=PipelineRunResponse)
def run_pipeline(payload: PipelineRunRequest) -> PipelineRunResponse:
    """대시보드의 "등록하기" 버튼 — 즉시 응답하고 실제 작업은 Celery 워커가 백그라운드로 처리한다."""
    options = payload.model_dump(exclude={"url"})
    task = run_pipeline_for_url_task.delay(payload.url, options)
    return PipelineRunResponse(task_id=task.id)


@router.post("/refresh-all", response_model=PipelineRunResponse)
def refresh_all_pipeline() -> PipelineRunResponse:
    """대시보드의 "전체 갱신" 버튼 — 이미 등록된 상품 전부를 저장된 URL로 다시 처리한다.

    코드가 고쳐진 뒤 예전에 등록해둔 상품들에 예전 값이 남아있을 때, 사용자가 상품마다
    URL을 일일이 다시 넣지 않고 한 번의 클릭으로 전부 최신화할 수 있게 해준다.
    """
    task = refresh_all_registered_products_task.delay()
    return PipelineRunResponse(task_id=task.id)


@router.get("/status/{task_id}")
def get_pipeline_status(task_id: str) -> dict:
    """대시보드가 몇 초 간격으로 이 API를 호출(polling)해서 진행 상태를 표시한다."""
    result = AsyncResult(task_id, app=celery_app)
    response: dict = {"task_id": task_id, "state": result.state}

    if result.state == "PROGRESS" and isinstance(result.info, dict):
        response["meta"] = result.info
    elif result.state == "SUCCESS":
        response["result"] = result.result
    elif result.state == "FAILURE":
        response["error"] = str(result.info)

    return response
