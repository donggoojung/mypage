"""MusinsaScraper 파싱 로직 검증 (2026-09-23 실 상품 상세 페이지 HTML로 검증됨).

abc_mart 스크래퍼 테스트와 동일한 방식 — 실제 musinsa.com 대신 로컬 가상 HTML
픽스처로 JSON-LD/DOM 파싱 "로직"만 검증한다. 픽스처는 실제로 캡처한 PDP 구조
(styled-components class*= 패턴, 품번 dt/dd 표, 사이즈 드롭다운이 열렸을 때의
품절/재고수량 표시)를 최대한 그대로 흉내낸다.
"""

from pathlib import Path

import pytest

from app.integrations.scrapers.musinsa import MusinsaScraper

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _file_url(name: str) -> str:
    return (FIXTURES_DIR / name).resolve().as_uri()


@pytest.mark.asyncio
async def test_parses_json_ld_product_data():
    scraper = MusinsaScraper(headless=True, min_delay=0, max_delay=0)
    url = _file_url("musinsa_product_jsonld.html")

    product = await scraper.fetch_product_by_url(url)

    assert product.brand_name == "나이키"
    assert product.style_code == "CW2288-111"
    assert product.price == 139000.0
    # 실사이트로 확인됨(2026-09-23): 품절 항목은 stock=0, 구매가능 항목은
    # "N개 남음"에서 읽은 정확한 재고 수량이 담긴다.
    assert product.size_stock["250"] == {"stock": 5, "is_sold_out": False}
    assert product.size_stock["260"] == {"stock": 0, "is_sold_out": True}
    assert product.size_stock["270"] == {"stock": 2, "is_sold_out": False}


@pytest.mark.asyncio
async def test_falls_back_to_dom_selectors_when_no_json_ld():
    scraper = MusinsaScraper(headless=True, min_delay=0, max_delay=0)
    url = _file_url("musinsa_product_dom_fallback.html")

    product = await scraper.fetch_product_by_url(url)

    assert product.brand_name == "아디다스"
    assert "슈퍼스타" in product.product_name
    # 실사이트로 확인됨(2026-09-23): 품번은 "정보" 탭의 dt/dd 표에서 정확히 읽어온다
    # (상품명에서 정규식으로 추출하는 것보다 정확함).
    assert product.style_code == "EG4958-001"
    assert product.price == 99000.0
    assert product.size_stock["240"] == {"stock": 4, "is_sold_out": False}
    assert product.size_stock["250"] == {"stock": 0, "is_sold_out": True}
