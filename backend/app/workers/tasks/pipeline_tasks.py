import asyncio
from decimal import Decimal

from app.core.celery_app import celery_app
from app.core.database import SessionLocalSync
from app.models.enums import SourcePlatform
from app.models.source_mapping import SourceMapping
from app.services.product_pipeline import PipelineOptions, PipelineResult, run_pipeline_for_url


def _options_from_dict(options: dict | None) -> PipelineOptions:
    options = options or {}
    return PipelineOptions(
        headless=True,  # 서버에서 도는 백그라운드 작업이라 항상 창 없이 실행.
        fixed_margin=Decimal(str(options.get("fixed_margin", "5000"))),
        target_margin_rate=Decimal(str(options.get("target_margin_rate", "0.30"))),
        customer_shipping_charge=Decimal(str(options.get("customer_shipping_charge", "3000"))),
        source_shipping_cost=Decimal(str(options.get("source_shipping_cost", "0"))),
        display_category_code=int(options.get("display_category_code", 56137)),
        category=options.get("category", "운동화"),
        color_tone=options.get("color_tone", "neutral"),
        request_approval=bool(options.get("request_approval", False)),
    )


def _result_to_dict(result: PipelineResult) -> dict:
    return {
        "url": result.url,
        "style_code": result.style_code,
        "brand_name": result.brand_name,
        "product_name": result.product_name,
        "purchase_cost": float(result.purchase_cost),
        "selling_price": float(result.selling_price),
        "thumbnail_url": result.thumbnail_url,
        "listing_id": result.listing_id,
        "market_product_id": result.market_product_id,
        "listing_status": result.listing_status,
    }


@celery_app.task(name="pipeline_tasks.run_pipeline_for_url", bind=True)
def run_pipeline_for_url_task(self, url: str, options: dict | None = None) -> dict:
    """웹 대시보드의 "등록하기" 버튼이 호출하는 태스크 — CLI 스크립트와 동일한
    app.services.product_pipeline 로직을 백그라운드로 실행하고 결과를 JSON으로 반환한다.

    Playwright 크롤링 + AI 이미지 생성은 몇 초~몇십 초가 걸릴 수 있어 HTTP 요청을
    기다리게 하지 않고 Celery 워커에서 비동기로 처리한다. 브라우저는
    GET /api/pipeline/status/{task_id}로 진행 상태를 폴링한다.
    """
    self.update_state(state="PROGRESS", meta={"step": "크롤링 및 등록 진행 중"})
    result = asyncio.run(run_pipeline_for_url(url, _options_from_dict(options)))
    return _result_to_dict(result)


@celery_app.task(name="pipeline_tasks.refresh_all_registered_products", bind=True)
def refresh_all_registered_products_task(self, options: dict | None = None) -> dict:
    """대시보드의 "전체 갱신" 버튼 — 이미 등록된 상품 전부를, 각자 저장된 원본 URL로
    다시 크롤링→가격재계산→AI이미지재생성→쿠팡 재등록한다.

    코드가 고쳐진 뒤(예: 이미지 저장 방식, 상태값 버그 수정) 예전에 등록해둔 상품들에는
    예전 방식으로 저장된 값이 그대로 남아있는데, 이걸 사용자가 상품마다 URL을 일일이
    다시 붙여넣지 않고 버튼 한 번으로 전부 최신 상태로 맞출 수 있게 해준다.
    """
    with SessionLocalSync() as session:
        urls = [
            row.source_url
            for row in session.query(SourceMapping).filter_by(source_platform=SourcePlatform.ABC_MART).all()
            if row.source_url
        ]

    pipeline_options = _options_from_dict(options)
    succeeded: list[dict] = []
    failed: list[dict] = []

    for idx, url in enumerate(urls, start=1):
        self.update_state(state="PROGRESS", meta={"step": f"({idx}/{len(urls)}) 갱신 중: {url}"})
        try:
            result = asyncio.run(run_pipeline_for_url(url, pipeline_options))
            succeeded.append(_result_to_dict(result))
        except Exception as exc:
            failed.append({"url": url, "error": str(exc)})

    return {"total": len(urls), "succeeded": succeeded, "failed": failed}
