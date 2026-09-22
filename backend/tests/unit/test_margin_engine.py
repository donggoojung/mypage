from decimal import Decimal

import pytest

from app.models.enums import MarketType
from app.services.margin_engine import (
    COUPANG_MINIMUM_FIXED_MARGIN,
    COUPANG_OUTBOUND_SHIPPING_COST,
    MarginInputs,
    PLATFORM_DEFAULT_FEE_RATES,
    ReverseMarginError,
    calculate_actual_net_profit,
    calculate_coupang_selling_price,
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


# --- 2026-09-22 사용자 확정 쿠팡 구간별 목표마진율 + 최소 고정마진 정책 -----------------


def _net_profit_for_coupang(purchase_cost: Decimal, price: Decimal) -> Decimal:
    inputs = MarginInputs(
        purchase_cost=purchase_cost,
        market_fee_rate=PLATFORM_DEFAULT_FEE_RATES[MarketType.COUPANG],
        source_shipping_cost=COUPANG_OUTBOUND_SHIPPING_COST,
    )
    return calculate_actual_net_profit(price, inputs)


@pytest.mark.parametrize(
    "purchase_cost,expected_rate",
    [
        (Decimal("30000"), Decimal("0.30")),  # 5만원 이하
        (Decimal("50000"), Decimal("0.30")),  # 경계값(이하 포함)
        (Decimal("70000"), Decimal("0.25")),  # 5~9.9만원
        (Decimal("99000"), Decimal("0.25")),  # 경계값
        (Decimal("120000"), Decimal("0.20")),  # 10~15만원
        (Decimal("150000"), Decimal("0.20")),  # 경계값
        (Decimal("200000"), Decimal("0.15")),  # 15만원 초과
    ],
)
def test_calculate_coupang_selling_price_uses_tiered_rate_when_above_fixed_floor(purchase_cost, expected_rate):
    """매입원가가 커서 %마진만으로도 최소 고정마진(1만원)을 넘길 때는 구간별 목표마진율 가격이 채택되어야 한다."""
    price = calculate_coupang_selling_price(purchase_cost)

    expected_price = calculate_selling_price_for_platform(
        market_type=MarketType.COUPANG,
        purchase_cost=purchase_cost,
        target_margin_rate=expected_rate,
        source_shipping_cost=COUPANG_OUTBOUND_SHIPPING_COST,
    )
    assert price == expected_price


def test_calculate_coupang_selling_price_fixed_floor_wins_for_cheap_item():
    """매입원가가 낮으면(예: 1만원) %마진만으로는 실이익이 1만원에 못 미쳐, 최소 고정마진 가격이 채택되어야 한다."""
    purchase_cost = Decimal("10000")
    price = calculate_coupang_selling_price(purchase_cost)

    net_profit = _net_profit_for_coupang(purchase_cost, price)
    assert net_profit >= COUPANG_MINIMUM_FIXED_MARGIN
    # 고정마진 플로어가 채택됐다면, 실이익은 플로어 금액에 근접해야 한다(원단위 올림 오차만 존재).
    assert net_profit - COUPANG_MINIMUM_FIXED_MARGIN < Decimal("1")


def test_calculate_coupang_selling_price_never_falls_below_minimum_fixed_margin():
    """어떤 매입원가를 넣어도 실제 이익이 최소 고정마진(1만원) 밑으로 내려가면 안 된다(역마진 방지 핵심 보증)."""
    for purchase_cost in [Decimal("1000"), Decimal("10000"), Decimal("49000"), Decimal("50001"), Decimal("500000")]:
        price = calculate_coupang_selling_price(purchase_cost)
        net_profit = _net_profit_for_coupang(purchase_cost, price)
        assert net_profit >= COUPANG_MINIMUM_FIXED_MARGIN


def test_calculate_coupang_selling_price_matches_confirmed_business_numbers():
    """사용자가 확정한 실제 예시 수치(원가 1만원/4.9만원)로 결과값을 고정 검증한다."""
    assert calculate_coupang_selling_price(Decimal("10000")) == Decimal("27236")
    assert calculate_coupang_selling_price(Decimal("49000")) == Decimal("91191")
