"""상세페이지 HTML 렌더링 + WebP 변환 엔진 (PRD 3.3).

원본 소싱처 레이아웃을 일절 쓰지 않고, 정제된 스펙 데이터 + AI 생성 이미지를
자체 템플릿(app/templates/detail_page.html)에 주입해 가로 860px WebP 이미지로 렌더링한다.
"""

import base64
import io
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from PIL import Image
from playwright.async_api import async_playwright

from app.integrations.scrapers.stealth import chromium_launch_kwargs

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
DETAIL_PAGE_WIDTH = 860

DEFAULT_WARRANTY_NOTICE = (
    "본 상품은 국내 정식 유통 채널에서 정상적으로 매입한 진정상품입니다.\n"
    "구매 영수증(매출전표)이 전산에 보관되어 있어 정품 문의 시 확인 가능합니다."
)
DEFAULT_SHIPPING_NOTICE = (
    "제품 하자·오배송의 경우 무료로 교환/반품해드립니다.\n"
    "단순 변심에 의한 교환/반품 시 왕복 배송비가 발생할 수 있습니다."
)


@dataclass
class DetailPageInput:
    brand_name: str
    product_name: str
    style_code: str
    specs: dict[str, str]
    size_stock: dict[str, dict]
    hero_image_bytes: bytes
    warranty_notice: str = DEFAULT_WARRANTY_NOTICE
    shipping_notice: str = DEFAULT_SHIPPING_NOTICE


class DetailPageRenderer:
    """스펙 데이터 + AI 생성 이미지를 받아 자체 HTML/CSS 상세페이지를 WebP로 렌더링한다."""

    def __init__(self, width: int = DETAIL_PAGE_WIDTH):
        self._width = width
        self._env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)

    def build_html(self, data: DetailPageInput) -> str:
        template = self._env.get_template("detail_page.html")
        return template.render(
            brand_name=data.brand_name,
            product_name=data.product_name,
            style_code=data.style_code,
            specs=list(data.specs.items()),
            size_stock=data.size_stock,
            hero_image_data_uri=self._to_data_uri(data.hero_image_bytes),
            warranty_notice=data.warranty_notice,
            shipping_notice=data.shipping_notice,
        )

    async def render_to_webp(self, html: str) -> bytes:
        """HTML을 Playwright로 렌더링한 뒤 전체 페이지를 캡처해 WebP로 변환한다."""
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**chromium_launch_kwargs(headless=True))
            try:
                page = await browser.new_page(viewport={"width": self._width, "height": 800})
                await page.set_content(html, wait_until="load")
                png_bytes = await page.screenshot(full_page=True, type="png")
            finally:
                await browser.close()

        return self._png_to_webp(png_bytes)

    @staticmethod
    def _to_data_uri(image_bytes: bytes) -> str:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        return f"data:image/png;base64,{b64}"

    @staticmethod
    def _png_to_webp(png_bytes: bytes) -> bytes:
        # PRD 3.3: 메타데이터(EXIF) 세탁 — 픽셀만 새 이미지 객체로 옮겨 저장해 원본 메타데이터를 제거한다.
        image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        clean_image = Image.new("RGB", image.size)
        clean_image.paste(image)
        buf = io.BytesIO()
        clean_image.save(buf, format="WEBP", quality=90)
        return buf.getvalue()
