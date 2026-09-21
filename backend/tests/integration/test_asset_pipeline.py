import io

import pytest
from PIL import Image

from app.services.asset_pipeline import generate_product_assets


def _sample_png() -> bytes:
    img = Image.new("RGB", (300, 300), (30, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_generate_product_assets_end_to_end_with_mocks():
    """USE_MOCK_AI_VISION/USE_MOCK_STORAGE 기본값(Mock)으로 전체 파이프라인을 검증한다."""
    result = await generate_product_assets(
        style_code="CW2288-111",
        brand_name="나이키",
        product_name="에어포스 1 '07 화이트",
        category="운동화",
        color_tone="white",
        specs={"소재": "천연가죽", "제조국": "베트남"},
        size_stock={"250": {"stock": 5, "is_sold_out": False}},
        source_image_bytes=_sample_png(),
    )

    assert result.thumbnail_url.endswith("products/CW2288-111/thumbnail.png")
    assert result.detail_page_url.endswith("products/CW2288-111/detail.webp")
    # Mock 스토리지는 app/static/generated/에 실제 파일로 저장하고, 대시보드가 바로
    # 열 수 있는 상대경로(/generated/...)를 돌려준다 (가짜 https:// CDN 주소가 아니다).
    assert result.thumbnail_url.startswith("/generated/")
    assert result.detail_page_url.startswith("/generated/")
