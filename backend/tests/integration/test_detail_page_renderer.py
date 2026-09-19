import io

import pytest
from PIL import Image

from app.services.detail_page_renderer import DetailPageInput, DetailPageRenderer


def _sample_png() -> bytes:
    img = Image.new("RGB", (400, 400), (220, 80, 80))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _sample_input() -> DetailPageInput:
    return DetailPageInput(
        brand_name="나이키",
        product_name="에어포스 1 '07 화이트",
        style_code="CW2288-111",
        specs={"소재": "천연가죽", "제조국": "베트남", "굽높이": "3cm"},
        size_stock={
            "250": {"stock": 5, "is_sold_out": False},
            "260": {"stock": 0, "is_sold_out": True},
        },
        hero_image_bytes=_sample_png(),
    )


def test_build_html_includes_product_data():
    renderer = DetailPageRenderer()
    html = renderer.build_html(_sample_input())

    assert "나이키" in html
    assert "CW2288-111" in html
    assert "천연가죽" in html
    assert "data:image/png;base64," in html
    assert "품절" in html  # 사이즈 260 품절 표시


@pytest.mark.asyncio
async def test_render_to_webp_produces_valid_webp_image():
    renderer = DetailPageRenderer()
    html = renderer.build_html(_sample_input())

    webp_bytes = await renderer.render_to_webp(html)

    image = Image.open(io.BytesIO(webp_bytes))
    assert image.format == "WEBP"
    assert image.width == 860
    assert image.height > 0
    # EXIF 등 메타데이터가 제거된 새 이미지여야 한다.
    assert not image.getexif()
