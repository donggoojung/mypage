from decimal import Decimal

import pytest

from app.models.enums import SourcePlatform
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.services.product_pipeline import PipelineResult
from app.workers.tasks import pipeline_tasks


@pytest.fixture
def two_registered_products(db_session):
    for i, style_code in enumerate(("REFRESH-TEST-001", "REFRESH-TEST-002")):
        product = MasterProduct(style_code=style_code, brand_name="테스트", product_name=f"테스트상품{i}")
        db_session.add(product)
        db_session.flush()
        db_session.add(
            SourceMapping(
                product_id=product.product_id,
                source_platform=SourcePlatform.ABC_MART,
                source_url=f"https://abcmart.a-rt.com/product?prdtNo={i}",
                source_price=59000,
                size_stock_json={"270": {"stock": 1, "is_sold_out": False}},
            )
        )
    db_session.commit()


def _fake_result(url: str) -> PipelineResult:
    return PipelineResult(
        url=url,
        style_code="REFRESH-TEST",
        brand_name="테스트",
        product_name="테스트상품",
        purchase_cost=Decimal("59000"),
        selling_price=Decimal("100000"),
        thumbnail_url="/generated/products/REFRESH-TEST/thumbnail.png",
        listing_id=1,
        market_product_id="12345",
        listing_status="draft",
    )


def test_refresh_all_registered_products_processes_every_stored_url(two_registered_products, monkeypatch):
    calls = []

    async def _fake_run_pipeline_for_url(url, options):
        calls.append(url)
        return _fake_result(url)

    monkeypatch.setattr(pipeline_tasks, "run_pipeline_for_url", _fake_run_pipeline_for_url)

    # .apply()로 실행해야 bind=True 태스크 안의 self.update_state()가 쓸 task_id가 생긴다
    # (.run()을 직접 부르면 태스크 컨텍스트가 없어 task_id=None으로 에러난다).
    result = pipeline_tasks.refresh_all_registered_products_task.apply().get()

    assert result["total"] == 2
    assert len(result["succeeded"]) == 2
    assert result["failed"] == []
    assert sorted(calls) == [
        "https://abcmart.a-rt.com/product?prdtNo=0",
        "https://abcmart.a-rt.com/product?prdtNo=1",
    ]


def test_refresh_all_registered_products_continues_past_one_failure(two_registered_products, monkeypatch):
    async def _fake_run_pipeline_for_url(url, options):
        if url.endswith("0"):
            raise RuntimeError("크롤링 실패 시뮬레이션")
        return _fake_result(url)

    monkeypatch.setattr(pipeline_tasks, "run_pipeline_for_url", _fake_run_pipeline_for_url)

    result = pipeline_tasks.refresh_all_registered_products_task.apply().get()

    assert result["total"] == 2
    assert len(result["succeeded"]) == 1
    assert len(result["failed"]) == 1
    assert "크롤링 실패 시뮬레이션" in result["failed"][0]["error"]


def test_refresh_all_registered_products_handles_no_products(db_session, monkeypatch):
    async def _fake_run_pipeline_for_url(url, options):
        raise AssertionError("호출되면 안 됨")

    monkeypatch.setattr(pipeline_tasks, "run_pipeline_for_url", _fake_run_pipeline_for_url)

    result = pipeline_tasks.refresh_all_registered_products_task.run()

    assert result == {"total": 0, "succeeded": [], "failed": []}
