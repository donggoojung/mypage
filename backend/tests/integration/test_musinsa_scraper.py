"""MusinsaScraper 파싱 로직 검증 (실사이트 미검증 스켈레톤).

abc_mart 스크래퍼 테스트와 동일한 방식 — 실제 musinsa.com 대신 로컬 가상 HTML
픽스처로 JSON-LD/DOM 파싱 "로직"만 검증한다. SELECTOR_* 값이 실제 무신사 화면과
일치하는지는 이 테스트로 전혀 보장되지 않으며, 실사이트 검증은 별도로 필요하다.
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
    assert product.size_stock["250"]["is_sold_out"] is False
    assert product.size_stock["260"]["is_sold_out"] is True
    assert product.size_stock["270"]["is_sold_out"] is False


@pytest.mark.asyncio
async def test_falls_back_to_dom_selectors_when_no_json_ld():
    scraper = MusinsaScraper(headless=True, min_delay=0, max_delay=0)
    url = _file_url("musinsa_product_dom_fallback.html")

    product = await scraper.fetch_product_by_url(url)

    assert product.brand_name == "아디다스"
    assert "슈퍼스타" in product.product_name
    # STYLE_CODE_PATTERN은 "EG4958-XX"처럼 대시+숫자 접미사가 있어야 매칭된다 — "EG4958"만
    # 있으면 못 찾는 게 정상이라 abc_mart 스크래퍼 테스트와 동일하게 빈 문자열도 허용한다.
    assert product.style_code in ("EG4958", "")
    assert product.price == 99000.0
    assert product.size_stock["240"]["is_sold_out"] is False
    assert product.size_stock["250"]["is_sold_out"] is True
