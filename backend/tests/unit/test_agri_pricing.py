"""농산물 가격 계산과 표시 문구 테스트."""

from types import SimpleNamespace

import pytest

from app.services import agri_pricing as pricing


def test_recommended_price_keeps_fruit_min_profit_after_fee():
    settings = pricing.merge_settings(None)
    rows = pricing.price_table(supply_price=15000, shipping_fee=0, category="fruit", settings=settings)
    for row in rows:
        assert row["price"] % 100 == 0
        assert row["profit"] >= 4000, row
        assert not row["below_min"]
    smartstore = next(r for r in rows if r["market"] == "smartstore")
    # (15000 + 4000) / (1 - 0.064) = 20299.1 → 100원 올림 20,300
    assert smartstore["recommended_price"] == 20300


def test_shipping_fee_is_part_of_cost():
    settings = pricing.merge_settings(None)
    without = pricing.price_table(15000, 0, "fruit", settings)[0]["recommended_price"]
    with_fee = pricing.price_table(15000, 3500, "fruit", settings)[0]["recommended_price"]
    assert with_fee > without


def test_manual_price_below_min_profit_is_flagged():
    settings = pricing.merge_settings(None)
    rows = pricing.price_table(15000, 0, "fruit", settings, sale_price=17000)
    assert all(r["price"] == 17000 for r in rows)
    assert all(r["below_min"] for r in rows)


def test_exact_division_does_not_round_up_extra_unit():
    assert pricing.recommended_price(cost=9000, min_profit=1000, fee_rate_percent=0) == 10000


def test_fee_rate_100_percent_raises():
    with pytest.raises(ValueError):
        pricing.recommended_price(1000, 1000, 100)


def test_merge_settings_keeps_defaults_for_missing_keys():
    merged = pricing.merge_settings({"fee_rates": {"smartstore": 5.5}, "cs_phone": "010-0000-0000"})
    assert merged["fee_rates"]["smartstore"] == 5.5
    assert merged["fee_rates"]["coupang"] == pricing.DEFAULT_SETTINGS["fee_rates"]["coupang"]
    assert merged["min_profit"]["fruit"] == 4000
    assert merged["cs_phone"] == "010-0000-0000"


def test_order_profit():
    # 판매 20,300원, 수수료 6.4% = 1,299원, 원가 15,000원 → 이익 4,001원
    assert pricing.order_profit(20300, 15000, 1, 6.4) == 4001


def _product(category: str):
    option = SimpleNamespace(label="5kg 중과", weight="5kg", count="15~18과", grade="상")
    return SimpleNamespace(
        name="충주 사과",
        category=category,
        origin="국산(충북 충주)",
        producer="충주농원",
        storage="냉장 보관",
        packing_info=None,
        variety="추청",
        harvest_year="2026년",
        grade="특",
        options=[option],
    )


def test_notice_text_has_required_items():
    text = pricing.product_notice_text(_product("fruit"), pricing.merge_settings({"cs_phone": "010-1234-5678"}))
    for needed in ("품목 또는 명칭: 충주 사과", "원산지: 국산(충북 충주)", "5kg 중과 (5kg, 15~18과, 상)",
                   "010-1234-5678", "단순 변심", "24시간"):
        assert needed in text
    assert "양곡 표시사항]" not in text


def test_notice_text_for_rice_has_grain_labels():
    text = pricing.product_notice_text(_product("rice"), pricing.merge_settings(None))
    assert "[양곡 표시사항]" in text
    assert "품종: 추청" in text
    assert "생산연도: 2026년" in text
    assert "소비자 상담 전화번호: (입력 필요)" in text
