from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.generated_asset import GeneratedAsset
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct

router = APIRouter(prefix="/api/products", tags=["products"])


@router.get("")
async def list_products(session: AsyncSession = Depends(get_db)) -> list[dict]:
    """대시보드 "등록된 상품" 표에 쓰인다 — 상품 + 최신 AI 이미지 + 쿠팡 등록 상태를 합쳐서 반환."""
    products = (await session.execute(select(MasterProduct).order_by(MasterProduct.product_id.desc()))).scalars().all()

    output = []
    for product in products:
        asset = (
            await session.execute(select(GeneratedAsset).where(GeneratedAsset.product_id == product.product_id))
        ).scalar_one_or_none()
        listing = (
            await session.execute(select(MarketListing).where(MarketListing.product_id == product.product_id))
        ).scalar_one_or_none()

        competitor_gap_percent = None
        if listing and listing.competitor_lowest_price:
            competitor_gap_percent = (
                (float(listing.selling_price) - float(listing.competitor_lowest_price))
                / float(listing.competitor_lowest_price)
                * 100
            )

        output.append(
            {
                "product_id": product.product_id,
                "style_code": product.style_code,
                "brand_name": product.brand_name,
                "product_name": product.product_name,
                "thumbnail_url": asset.ai_thumbnail_url if asset else None,
                "selling_price": float(listing.selling_price) if listing else None,
                "listing_status": listing.status.value if listing else None,
                "market_product_id": listing.market_product_id if listing else None,
                "competitor_count": listing.competitor_count if listing else None,
                "competitor_lowest_price": float(listing.competitor_lowest_price)
                if listing and listing.competitor_lowest_price is not None
                else None,
                "competitor_gap_percent": competitor_gap_percent,
            }
        )
    return output
