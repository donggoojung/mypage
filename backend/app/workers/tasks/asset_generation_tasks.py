import asyncio

import httpx
from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.database import SessionLocalSync
from app.models.enums import GenerationStatus
from app.models.generated_asset import GeneratedAsset
from app.models.master_product import MasterProduct
from app.models.source_mapping import SourceMapping
from app.services.asset_pipeline import generate_product_assets


@celery_app.task(name="asset_generation_tasks.generate_assets_for_product")
def generate_assets_for_product(product_id: int, category: str = "shoes", color_tone: str = "neutral") -> dict:
    """PRD 3차 지시: master_products 1건에 대해 AI 이미지+상세페이지를 생성해
    generated_assets 테이블에 적재하는 백그라운드 작업.
    """
    with SessionLocalSync() as session:
        product = session.get(MasterProduct, product_id)
        if product is None:
            raise ValueError(f"product_id={product_id} 인 master_products 행이 없습니다.")

        source_mapping = session.scalar(
            select(SourceMapping).where(SourceMapping.product_id == product_id).order_by(SourceMapping.source_id)
        )
        image_url = source_mapping.source_image_url if source_mapping else None

        asset = session.scalar(select(GeneratedAsset).where(GeneratedAsset.product_id == product_id))
        if asset is None:
            asset = GeneratedAsset(product_id=product_id)
            session.add(asset)
        asset.generation_status = GenerationStatus.PROCESSING
        session.commit()

        try:
            source_image_bytes = asyncio.run(_load_source_image(image_url))
            result = asyncio.run(
                generate_product_assets(
                    style_code=product.style_code,
                    brand_name=product.brand_name,
                    product_name=product.product_name,
                    category=category,
                    color_tone=color_tone,
                    specs=product.raw_specs_json or {},
                    size_stock=(source_mapping.size_stock_json if source_mapping else {}) or {},
                    source_image_bytes=source_image_bytes,
                )
            )
        except Exception:
            asset.generation_status = GenerationStatus.FAILED
            session.commit()
            raise

        asset.ai_thumbnail_url = result.thumbnail_url
        asset.ai_detail_image_url = result.detail_page_url
        asset.exif_cleared = True
        asset.generation_status = GenerationStatus.COMPLETED
        session.commit()

        return {
            "asset_id": asset.asset_id,
            "product_id": product_id,
            "thumbnail_url": asset.ai_thumbnail_url,
            "detail_page_url": asset.ai_detail_image_url,
        }


async def _load_source_image(image_url: str | None) -> bytes:
    if not image_url:
        raise ValueError("소싱처 원본 이미지 URL이 없어 AI 이미지 생성을 진행할 수 없습니다.")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(image_url)
        response.raise_for_status()
        return response.content
