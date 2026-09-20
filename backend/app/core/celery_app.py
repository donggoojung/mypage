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
