"""전체 자산 생성 파이프라인 오케스트레이션 (PRD 3장).

원본 소싱처 이미지 → 객체 분할(누끼) → 배경 합성 → 상세페이지 렌더링 → S3 업로드까지
한 번에 처리한다. 각 단계는 Mock/Real 전환 가능한 독립 컴포넌트를 조합한 것뿐이라,
`USE_MOCK_AI_VISION`/`USE_MOCK_STORAGE` 설정에 따라 자동으로 동작 방식이 바뀐다.
"""

from dataclasses import dataclass

from app.integrations.ai_vision.factory import get_background_synthesizer, get_object_segmenter
from app.integrations.storage.s3_client import get_storage_client
from app.services.detail_page_renderer import DetailPageInput, DetailPageRenderer


@dataclass
class GeneratedProductAssets:
    thumbnail_url: str
    detail_page_url: str


async def generate_product_assets(
    style_code: str,
    brand_name: str,
    product_name: str,
    category: str,
    color_tone: str,
    specs: dict[str, str],
    size_stock: dict[str, dict],
    source_image_bytes: bytes,
) -> GeneratedProductAssets:
    """PRD 3.2(누끼+배경합성) → 3.3(상세페이지 렌더링) → 3.3(S3 업로드) 전 과정을 수행한다."""
    segmenter = get_object_segmenter()
    synthesizer = get_background_synthesizer()
    storage = get_storage_client()
    renderer = DetailPageRenderer()

    object_png = await segmenter.segment(source_image_bytes)
    thumbnail_png = await synthesizer.synthesize(object_png, category=category, color_tone=color_tone)

    thumbnail_url = storage.upload_bytes(
        thumbnail_png, key=f"products/{style_code}/thumbnail.png", content_type="image/png"
    )

    detail_input = DetailPageInput(
        brand_name=brand_name,
        product_name=product_name,
        style_code=style_code,
        specs=specs,
        size_stock=size_stock,
        hero_image_bytes=thumbnail_png,
    )
    html = renderer.build_html(detail_input)
    detail_webp = await renderer.render_to_webp(html)

    detail_page_url = storage.upload_bytes(
        detail_webp, key=f"products/{style_code}/detail.webp", content_type="image/webp"
    )

    return GeneratedProductAssets(thumbnail_url=thumbnail_url, detail_page_url=detail_page_url)
