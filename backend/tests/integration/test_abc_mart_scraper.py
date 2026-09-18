"""ABCMartScraper 파싱 로직 검증.

실제 abcmart.com은 이 개발 환경에서 네트워크 접속이 차단되어 있어(정책상),
로컬 고정 HTML 픽스처로 JSON-LD/DOM 파싱 "로직"을 검증한다. DOM 폴백 셀렉터가
실사이트와 100% 일치하는지는 이 테스트로 보장되지 않으며, 실사이트 검증은
로컬 PC에서 scripts/test_scraper_margin.py로 별도 수행해야 한다.
"""

from pathlib import Path

import pytest

from app.integrations.scrapers.abc_mart import ABCMartScraper

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _file_url(name: str) -> str:
    return (FIXTURES_DIR / name).resolve().as_uri()


@pytest.mark.asyncio
async def test_parses_json_ld_product_data():
    scraper = ABCMartScraper(headless=True, min_delay=0, max_delay=0)
    url = _file_url("abc_mart_product_jsonld.html")

    product = await scraper.fetch_product_by_url(url)

    assert product.brand_name == "나이키"
    assert product.style_code == "CW2288-111"
    assert product.price == 139000.0
    assert product.size_stock["250"]["is_sold_out"] is False
    assert product.size_stock["260"]["is_sold_out"] is True
    assert product.size_stock["270"]["is_sold_out"] is False


@pytest.mark.asyncio
async def test_falls_back_to_dom_selectors_when_no_json_ld():
    scraper = ABCMartScraper(headless=True, min_delay=0, max_delay=0)
    url = _file_url("abc_mart_product_dom_fallback.html")

    product = await scraper.fetch_product_by_url(url)

    assert product.brand_name == "아디다스"
    assert "슈퍼스타" in product.product_name
    assert product.style_code == "EG4958" or product.style_code == ""
    assert product.price == 99000.0
    assert product.size_stock["240"]["is_sold_out"] is False
    assert product.size_stock["250"]["is_sold_out"] is True


@pytest.mark.asyncio
async def test_fetch_product_by_style_code_not_implemented():
    scraper = ABCMartScraper()
    with pytest.raises(NotImplementedError):
        await scraper.fetch_product("CW2288-111")
