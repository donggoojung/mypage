from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "brand_fulfillment",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.workers.tasks.crawl_tasks",
        "app.workers.tasks.asset_generation_tasks",
        "app.workers.tasks.order_tasks",
        "app.workers.tasks.pipeline_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Seoul",
    enable_utc=True,
    task_track_started=True,
)

# 정기 실행 스케줄 (PRD 5.1 "웹훅 미지원 채널은 5분 주기 스케줄러") —
# 실행하려면 워커와 별도로 `celery -A app.core.celery_app beat` 프로세스를 함께 띄워야 한다.
celery_app.conf.beat_schedule = {
    "detect-new-orders-every-5-minutes": {
        "task": "order_tasks.detect_new_orders",
        "schedule": 300.0,
    },
    # PRD 7장: 매입 완료 후 취소/반품 감지가 가장 치명적인 리스크(배송비/매입비 손실)라
    # 신규 주문 감지와 같은 5분 주기로 다룬다.
    "detect-cancellations-and-returns-every-5-minutes": {
        "task": "order_tasks.detect_cancellations_and_returns",
        "schedule": 300.0,
    },
    "refresh-source-stock-every-30-minutes": {
        "task": "crawl_tasks.refresh_all_source_mappings",
        "schedule": 1800.0,
    },
}
