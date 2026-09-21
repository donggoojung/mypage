import asyncio
from decimal import Decimal

from app.core.celery_app import celery_app
from app.services.product_pipeline import PipelineOptions, run_pipeline_for_url


@celery_app.task(name="pipeline_tasks.run_pipeline_for_url", bind=True)
def run_pipeline_for_url_task(self, url: str, options: dict | None = None) -> dict:
    """웹 대시보드의 "등록하기" 버튼이 호출하는 태스크 — CLI 스크립트와 동일한
    app.services.product_pipeline 로직을 백그라운드로 실행하고 결과를 JSON으로 반환한다.

    Playwright 크롤링 + AI 이미지 생성은 몇 초~몇십 초가 걸릴 수 있어 HTTP 요청을
    기다리게 하지 않고 Celery 워커에서 비동기로 처리한다. 브라우저는
    GET /api/pipeline/status/{task_id}로 진행 상태를 폴링한다.
    """
    options = options or {}
    pipeline_options = PipelineOptions(
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

    self.update_state(state="PROGRESS", meta={"step": "크롤링 및 등록 진행 중"})
    result = asyncio.run(run_pipeline_for_url(url, pipeline_options))

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
