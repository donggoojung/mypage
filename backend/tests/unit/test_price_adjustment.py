from dataclasses import replace
from decimal import Decimal

from app.services.margin_engine import MarginInputs, calculate_selling_price
from app.services.price_adjustment import recommend_price


def _inputs(**overrides) -> MarginInputs:
    base = dict(
        purchase_cost=Decimal("59000"),
        market_fee_rate=Decimal("0.1188"),  # 쿠팡
        fixed_margin=Decimal("5000"),
        customer_shipping_charge=Decimal("3000"),
        target_margin_rate=Decimal("0.30"),
    )
    base.update(overrides)
    return MarginInputs(**base)


def test_no_competitor_price_uses_engine_price_as_is():
    inputs = _inputs()
    result = recommend_price(inputs, competitor_price=None)

    assert result.competitor_price is None
    assert result.recommended_price == calculate_selling_price(inputs)
    assert result.matched_competitor is False


def test_engine_price_already_cheaper_than_competitor_keeps_engine_price():
    inputs = _inputs()
    engine_price = calculate_selling_price(inputs)
    higher_competitor_price = engine_price + Decimal("50000")

    result = recommend_price(inputs, competitor_price=higher_competitor_price)

    assert result.recommended_price == engine_price
    assert result.matched_competitor is False


def test_matches_competitor_when_fixed_margin_still_safe():
    inputs = _inputs()
    engine_price = calculate_selling_price(inputs)
    floor_price = calculate_selling_price(replace(inputs, target_margin_rate=Decimal("0")))
    # 마지노선보다는 높고, 계산가보다는 낮은 경쟁가.
    competitor_price = floor_price + Decimal("1000")
    assert competitor_price < engine_price

    result = recommend_price(inputs, competitor_price=competitor_price)

    assert result.matched_competitor is True
    assert result.recommended_price == competitor_price


def test_refuses_to_match_competitor_below_fixed_margin_floor():
    inputs = _inputs()
    floor_price = calculate_selling_price(replace(inputs, target_margin_rate=Decimal("0")))
    # 마지노선보다도 낮은, 도저히 따라갈 수 없는 경쟁가.
    dangerously_low_competitor_price = floor_price - Decimal("5000")

    result = recommend_price(inputs, competitor_price=dangerously_low_competitor_price)

    assert result.matched_competitor is False
    assert result.recommended_price == calculate_selling_price(inputs)
    assert result.recommended_price > dangerously_low_competitor_price


def test_minimum_safe_price_never_exceeds_engine_price():
    inputs = _inputs()
    result = recommend_price(inputs, competitor_price=Decimal("1"))

    assert result.minimum_safe_price <= result.engine_price
