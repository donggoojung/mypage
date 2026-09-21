from decimal import Decimal

import pytest

from app.models.enums import MarketType
from app.services.margin_engine import (
    MarginInputs,
    PLATFORM_DEFAULT_FEE_RATES,
    ReverseMarginError,
    calculate_actual_net_profit,
    calculate_selling_price,
    calculate_selling_price_for_platform,
    calculate_simple_markup_price,
)


def test_calculate_simple_markup_price_applies_flat_percentage_on_list_price():
    # 실사용 사례: 정가 129,000원짜리 상품을 30% 마크업해서 파는 경우.
    price = calculate_simple_markup_price(Decimal("129000"), Decimal("0.30"))

    assert price == Decimal("167700")  # 129000 * 1.30


def test_calculate_simple_markup_price_rounds_up_to_whole_won():
    price = calculate_simple_markup_price(Decimal("59000"), Decimal("0.30"))

    assert price == Decimal("76700")  # 59000 * 1.30 = 76700.0 정확히 나눠떨어지는 예시


def test_calculate_simple_markup_price_rounds_up_fractional_won():
    price = calculate_simple_markup_price(Decimal("59001"), Decimal("0.30"))

    # 59001 * 1.30 = 76701.3 → 원 단위 미만은 올림
    assert price == Decimal("76702")


def test_calculate_simple_markup_price_defaults_to_30_percent():
    price = calculate_simple_markup_price(Decimal("100000"))

    assert price == Decimal("130000")


def test_calculate_selling_price_matches_manual_worked_example():
    # PRD 4.1 예시 수치: 매입원가 80,000 / 소싱처배송비 0 / 고객청구배송비 3,000 /
    # 고정마진 5,000 / 쿠팡 수수료 11.88% / 목표마진율 15% / 부가세 실효율 0%
    inputs = MarginInputs(
        purchase_cost=Decimal("80000"),
        market_fee_rate=Decimal("0.1188"),
        fixed_margin=Decimal("5000"),
        source_shipping_cost=Decimal("0"),
        customer_shipping_charge=Decimal("3000"),
        target_margin_rate=Decimal("0.15"),
        effective_vat_rate=Decimal("0"),
    )

    price = calculate_selling_price(inputs)

    numerator = Decimal("80000") + Decimal("0") - Decimal("3000") + Decimal("5000")
    denominator = Decimal("1") - (Decimal("0.1188") + Decimal("0.15") + Decimal("0"))
    expected = (numerator / denominator).quantize(Decimal("1"), rounding="ROUND_CEILING")
    assert price == expected


@pytest.mark.parametrize(
    "purchase_cost,fixed_margin,source_shipping_cost,customer_shipping_charge,target_margin_rate",
    [
        (Decimal("50000"), Decimal("5000"), Decimal("0"), Decimal("3000"), Decimal("0.15")),
        (Decimal("120000"), Decimal("7000"), Decimal("3000"), Decimal("3000"), Decimal("0.20")),
        (Decimal("9900"), Decimal("2000"), Decimal("0"), Decimal("0"), Decimal("0.10")),
    ],
)
@pytest.mark.parametrize("market_type", list(PLATFORM_DEFAULT_FEE_RATES.keys()))
def test_no_reverse_margin_for_all_platforms(
    market_type, purchase_cost, fixed_margin, source_shipping_cost, customer_shipping_charge, target_margin_rate
):
    """어떤 플랫폼·입력값 조합이어도 실제 순이익이 고정마진 이상이어야 한다 (역마진 방지 핵심 보증)."""
    price = calculate_selling_price_for_platform(
        market_type=market_type,
        purchase_cost=purchase_cost,
        fixed_margin=fixed_margin,
        source_shipping_cost=source_shipping_cost,
        customer_shipping_charge=customer_shipping_charge,
        target_margin_rate=target_margin_rate,
    )

    inputs = MarginInputs(
        purchase_cost=purchase_cost,
        market_fee_rate=PLATFORM_DEFAULT_FEE_RATES[market_type],
        fixed_margin=fixed_margin,
        source_shipping_cost=source_shipping_cost,
        customer_shipping_charge=customer_shipping_charge,
        target_margin_rate=target_margin_rate,
    )
    net_profit = calculate_actual_net_profit(price, inputs)

    assert net_profit >= fixed_margin


def test_platform_default_fee_rates_match_prd():
    assert PLATFORM_DEFAULT_FEE_RATES[MarketType.NAVER_SMARTSTORE] == Decimal("0.0563")
    assert PLATFORM_DEFAULT_FEE_RATES[MarketType.COUPANG] == Decimal("0.1188")
    assert PLATFORM_DEFAULT_FEE_RATES[MarketType.GMARKET] == Decimal("0.13")


def test_unknown_platform_without_explicit_fee_rate_raises():
    with pytest.raises(ValueError, match="toss"):
        calculate_selling_price_for_platform(
            market_type=MarketType.TOSS,
            purchase_cost=Decimal("10000"),
            fixed_margin=Decimal("1000"),
        )


def test_explicit_fee_rate_overrides_platform_default():
    price_default = calculate_selling_price_for_platform(
        market_type=MarketType.COUPANG,
        purchase_cost=Decimal("50000"),
        fixed_margin=Decimal("5000"),
    )
    price_override = calculate_selling_price_for_platform(
        market_type=MarketType.COUPANG,
        purchase_cost=Decimal("50000"),
        fixed_margin=Decimal("5000"),
        market_fee_rate=Decimal("0.05"),
    )

    assert price_override != price_default
    assert price_override < price_default  # 수수료율이 낮을수록 판매가는 낮아져야 한다


def test_rate_sum_over_100_percent_raises_reverse_margin_error():
    inputs = MarginInputs(
        purchase_cost=Decimal("10000"),
        market_fee_rate=Decimal("0.5"),
        target_margin_rate=Decimal("0.4"),
        effective_vat_rate=Decimal("0.2"),  # 합계 1.1 > 1
    )

    with pytest.raises(ReverseMarginError):
        calculate_selling_price(inputs)


def test_price_is_rounded_up_to_whole_won():
    inputs = MarginInputs(purchase_cost=Decimal("10001"), market_fee_rate=Decimal("0.1"))
    price = calculate_selling_price(inputs)

    assert price == price.to_integral_value()  # 소수점 없는 정수 원 단위
