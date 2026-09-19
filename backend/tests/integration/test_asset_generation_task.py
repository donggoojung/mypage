import io

import pytest
from PIL import Image
from sqlalchemy import select

from app.models.enums import GenerationStatus, SourcePlatform
from app.models.generated_asset import GeneratedAsset
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.workers.tasks import asset_generation_tasks


def _sample_png() -> bytes:
    img = Image.new("RGB", (200, 200), (80, 160, 80))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def sample_product(db_session):
    product = MasterProduct(
        style_code="TEST-0001",
        brand_name="테스트브랜드",
        product_name="테스트 운동화",
        raw_specs_json={"소재": "합성섬유"},
    )
    db_session.add(product)
    db_session.flush()

    mapping = SourceMapping(
        product_id=product.product_id,
        source_platform=SourcePlatform.ABC_MART,
        source_url="https://mock.abcmart.example.com/products/TEST-0001",
        source_image_url="https://mock.abcmart.example.com/images/TEST-0001.jpg",
        source_price=59000,
        size_stock_json={"270": {"stock": 3, "is_sold_out": False}},
    )
    db_session.add(mapping)
    db_session.commit()
    return product


def test_generate_assets_for_product_writes_generated_asset_row(monkeypatch, sample_product, db_session):
    async def _fake_load_source_image(image_url: str | None) -> bytes:
        assert image_url == "https://mock.abcmart.example.com/images/TEST-0001.jpg"
        return _sample_png()

    monkeypatch.setattr(asset_generation_tasks, "_load_source_image", _fake_load_source_image)

    result = asset_generation_tasks.generate_assets_for_product(sample_product.product_id)

    assert result["thumbnail_url"].endswith("products/TEST-0001/thumbnail.png")
    assert result["detail_page_url"].endswith("products/TEST-0001/detail.webp")

    asset = db_session.scalar(select(GeneratedAsset).where(GeneratedAsset.product_id == sample_product.product_id))
    assert asset is not None
    assert asset.generation_status == GenerationStatus.COMPLETED
    assert asset.exif_cleared is True
    assert asset.ai_thumbnail_url == result["thumbnail_url"]


def test_generate_assets_for_product_raises_for_unknown_product():
    with pytest.raises(ValueError, match="product_id"):
        asset_generation_tasks.generate_assets_for_product(999999)
