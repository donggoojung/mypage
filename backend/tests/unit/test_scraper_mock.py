import pytest

from app.integrations.scrapers.mock_scraper import MockScraper


@pytest.fixture
def scraper():
    return MockScraper()


@pytest.mark.asyncio
async def test_fetch_known_style_code_returns_catalog_data(scraper):
    product = await scraper.fetch_product("CW2288-111")

    assert product.style_code == "CW2288-111"
    assert product.brand_name == "나이키"
    assert product.price == 139000.0
    assert product.size_stock["250"]["is_sold_out"] is False
    assert product.size_stock["260"]["is_sold_out"] is True


@pytest.mark.asyncio
async def test_fetch_unknown_style_code_returns_consistent_fake_data(scraper):
    product = await scraper.fetch_product("UNKNOWN-999")

    assert product.style_code == "UNKNOWN-999"
    assert product.price > 0
    assert "270" in product.size_stock
