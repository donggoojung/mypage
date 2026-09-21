from celery.result import AsyncResult
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.celery_app import celery_app
from app.workers.tasks.pipeline_tasks import (
    discover_category_urls_task,
    refresh_all_registered_products_task,
    run_pipeline_for_url_task,
    run_pipeline_for_urls_task,
)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class PipelineRunRequest(BaseModel):
    url: str
    request_approval: bool = False
    target_margin_rate: float = 0.30  # 정가 대비 마크업 비율 (판매가 = 원가 × (1+이 값))
    # None(기본값)이면 쿠팡 카테고리 자동추천 API로 상품명에 맞는 코드를 자동으로 찾는다.
    display_category_code: int | None = None
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


class PipelineRunBatchRequest(BaseModel):
    urls: list[str]
    request_approval: bool = False
    target_margin_rate: float = 0.30
    display_category_code: int | None = None
    category: str = "운동화"
    color_tone: str = "neutral"


@router.post("/run-batch", response_model=PipelineRunResponse)
def run_pipeline_batch(payload: PipelineRunBatchRequest) -> PipelineRunResponse:
    """대시보드의 "섹션 일괄 등록" 버튼 — 사용자가 검토한 URL 목록을 순서대로 신규 등록한다."""
    urls = [u.strip() for u in payload.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="등록할 URL이 없습니다.")
    options = payload.model_dump(exclude={"urls"})
    task = run_pipeline_for_urls_task.delay(urls, options)
    return PipelineRunResponse(task_id=task.id)


class DiscoverCategoryRequest(BaseModel):
    category_url: str
    max_products: int = 30
    max_pages: int = 1


@router.post("/discover", response_model=PipelineRunResponse)
def discover_category(payload: DiscoverCategoryRequest) -> PipelineRunResponse:
    """대시보드의 "URL 목록 가져오기" 버튼 — 카테고리/랭킹 페이지에서 상품 URL만 수집한다(등록 안 함)."""
    task = discover_category_urls_task.delay(payload.category_url, payload.max_products, payload.max_pages)
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
