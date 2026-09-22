"""역마진 방지 정밀 동적 가격 결정 엔진 (PRD 4.1).

Final Price = (P_cost + S_cost - S_charge + M_fixed) / (1 - (F_rate + M_rate + VAT_rate))

- P_cost: 소싱처 실제 매입 원가 (회원 등급 할인, 즉시할인 쿠폰 반영액)
- S_cost: 소싱처 부과 배송비 (통상 무료배송 기준 충족 시 0원)
- S_charge: 마켓에서 고객에게 부과하는 기본 배송비
- M_fixed: 상품 1건당 무조건 남겨야 하는 절대 고정 마진
- F_rate: 오픈마켓 정산 수수료율
- M_rate: 매출액 대비 목표 변동 마진율
- VAT_rate: 부가가치세 실효 공제율

돈 계산은 부동소수점 오차를 피하기 위해 전 구간 Decimal로 수행한다.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from app.models.enums import MarketType

# PRD 4.1 각주 6 기준 플랫폼별 기본 정산 수수료율.
# 쿠팡은 카테고리별로 10.8%~11.88% 범위이므로, 역마진 방지 목적상 보수적으로 상단값을 기본값으로 둔다.
# TOSS는 PRD에 명시된 기준 수수료율이 없어 기본값을 제공하지 않으며, 호출 시 market_fee_rate를 직접 지정해야 한다.
PLATFORM_DEFAULT_FEE_RATES: dict[MarketType, Decimal] = {
    MarketType.NAVER_SMARTSTORE: Decimal("0.0563"),
    MarketType.COUPANG: Decimal("0.1188"),
    MarketType.GMARKET: Decimal("0.13"),
}


class ReverseMarginError(ValueError):
    """수수료율 + 목표마진율 + 부가세율 합이 100% 이상이라 가격 산출이 불가능할 때 발생."""


@dataclass(frozen=True)
class MarginInputs:
    purchase_cost: Decimal
    market_fee_rate: Decimal
    fixed_margin: Decimal = Decimal("0")
    source_shipping_cost: Decimal = Decimal("0")
    customer_shipping_charge: Decimal = Decimal("0")
    target_margin_rate: Decimal = Decimal("0.30")
    effective_vat_rate: Decimal = Decimal("0")


def calculate_selling_price(inputs: MarginInputs) -> Decimal:
    """PRD 4.1 역마진 방지 수식으로 판매가를 산출한다.

    나눗셈 결과는 항상 원 단위로 올림(ROUND_CEILING) 처리한다 — 내림으로 반올림하면
    고정 마진이 원 단위 오차만큼 깎여 역마진 방지 원칙이 깨지기 때문이다.
    """
    rate_sum = inputs.market_fee_rate + inputs.target_margin_rate + inputs.effective_vat_rate
    denominator = Decimal("1") - rate_sum
    if denominator <= 0:
        raise ReverseMarginError(
            f"수수료율+목표마진율+부가세율 합({rate_sum})이 100% 이상이라 가격을 산출할 수 없습니다."
        )

    numerator = (
        inputs.purchase_cost
        + inputs.source_shipping_cost
        - inputs.customer_shipping_charge
        + inputs.fixed_margin
    )
    price = numerator / denominator
    return price.quantize(Decimal("1"), rounding=ROUND_CEILING)


def calculate_simple_markup_price(purchase_cost: Decimal, markup_rate: Decimal = Decimal("0.30")) -> Decimal:
    """정가(크롤링된 표시가) 대비 단순 마크업 판매가 = 원가 × (1 + markup_rate).

    `calculate_selling_price_for_platform`(역마진 방지 공식)과 달리 오픈마켓 수수료나
    배송비 상계를 반영하지 않는다 — "정가에 30% 얹어서 판다" 같은 단순한 가격 정책을
    쓰고 싶을 때 쓴다. 원 단위 미만은 올림 처리한다.
    """
    price = purchase_cost * (Decimal("1") + markup_rate)
    return price.quantize(Decimal("1"), rounding=ROUND_CEILING)


def calculate_selling_price_for_platform(
    market_type: MarketType,
    purchase_cost: Decimal,
    fixed_margin: Decimal = Decimal("0"),
    source_shipping_cost: Decimal = Decimal("0"),
    customer_shipping_charge: Decimal = Decimal("0"),
    target_margin_rate: Decimal = Decimal("0.30"),
    effective_vat_rate: Decimal = Decimal("0"),
    market_fee_rate: Decimal | None = None,
) -> Decimal:
    """플랫폼 기본 수수료율(PLATFORM_DEFAULT_FEE_RATES)을 사용해 판매가를 산출하는 편의 함수.

    market_fee_rate를 명시하면 기본값 대신 그 값을 사용한다 (카테고리별 우대 수수료 등).
    """
    fee_rate = market_fee_rate
    if fee_rate is None:
        if market_type not in PLATFORM_DEFAULT_FEE_RATES:
            raise ValueError(
                f"{market_type.value}의 기본 수수료율이 정의되어 있지 않습니다. "
                "market_fee_rate를 직접 지정해주세요."
            )
        fee_rate = PLATFORM_DEFAULT_FEE_RATES[market_type]

    inputs = MarginInputs(
        purchase_cost=purchase_cost,
        market_fee_rate=fee_rate,
        fixed_margin=fixed_margin,
        source_shipping_cost=source_shipping_cost,
        customer_shipping_charge=customer_shipping_charge,
        target_margin_rate=target_margin_rate,
        effective_vat_rate=effective_vat_rate,
    )
    return calculate_selling_price(inputs)


# --- 2026-09-22 사용자 확정 사업 규칙: 매입원가 구간별 목표마진율 + 최소 고정마진 -------
# 매입원가가 낮을수록(재고소진/역마진 리스크가 더 크므로) 목표마진율을 더 높게 잡는 정책.
# (임계값, 그 임계값 "이하"일 때 적용할 목표마진율) 순서로 낮은 구간부터 나열한다.
COUPANG_MARGIN_TIERS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("50000"), Decimal("0.30")),
    (Decimal("99000"), Decimal("0.25")),
    (Decimal("150000"), Decimal("0.20")),
)
# 위 구간 전부를 초과하는(15만원 초과) 매입원가에 적용할 목표마진율.
COUPANG_MARGIN_RATE_ABOVE_TOP_TIER = Decimal("0.15")
# 건당 실제 택배 배송비(무료배송으로 고객에게는 안 받지만, 판매자가 실제로 부담하는 비용).
COUPANG_OUTBOUND_SHIPPING_COST = Decimal("4000")
# %마진 계산 결과가 이보다 적게 남으면, 이 금액이 남도록 가격을 올린다.
COUPANG_MINIMUM_FIXED_MARGIN = Decimal("10000")
# 최종 판매가를 이 단위로 올림 처리한다 — 1,000원 미만 자리는 전부 "0"으로 끊어서
# 62,080원 같은 어중간한 가격 대신 63,000원처럼 깔끔한 가격으로 등록한다.
# (쿠팡 API는 10원 단위 미만 입력을 아예 거부하는데, 1,000원 단위면 그 조건도 자동으로 만족한다.)
COUPANG_PRICE_ROUNDING_UNIT = Decimal("1000")


def _tiered_target_margin_rate(purchase_cost: Decimal, tiers: tuple[tuple[Decimal, Decimal], ...]) -> Decimal:
    for threshold, rate in tiers:
        if purchase_cost <= threshold:
            return rate
    return COUPANG_MARGIN_RATE_ABOVE_TOP_TIER


def round_up_to_price_unit(price: Decimal, unit: Decimal) -> Decimal:
    """가격을 unit 단위로 올림한다 — 올림이라 실이익은 목표치보다 살짝 더 남으면 남았지
    부족해지진 않는다(역마진 방지 원칙 유지)."""
    return (price / unit).quantize(Decimal("1"), rounding=ROUND_CEILING) * unit


def calculate_coupang_selling_price(
    purchase_cost: Decimal,
    market_fee_rate: Decimal = PLATFORM_DEFAULT_FEE_RATES[MarketType.COUPANG],
    outbound_shipping_cost: Decimal = COUPANG_OUTBOUND_SHIPPING_COST,
    minimum_fixed_margin: Decimal = COUPANG_MINIMUM_FIXED_MARGIN,
    tiers: tuple[tuple[Decimal, Decimal], ...] = COUPANG_MARGIN_TIERS,
) -> Decimal:
    """사용자가 확정한 쿠팡 판매가 정책으로 판매가를 산출한다.

    목표마진율은 "매입원가 대비" 이익률이다(매출액 대비가 아님) — 즉 원가 39,000원에
    목표마진율 30%면, 실제로 남기려는 이익금은 "판매가의 30%"가 아니라
    "39,000원 × 30% = 11,700원"이다. 그래서 구간별 목표마진율로 이익금(원 단위)을
    먼저 구하고, "무조건 최소 고정마진(기본 1만원)은 남긴다"는 하한선과 비교해
    더 큰 금액을 최종 이익금으로 채택한 뒤, 그 이익금을 MarginInputs.fixed_margin으로
    넘겨 역마진 방지 공식(판매가 = (원가+택배비+이익금)/(1-수수료율))으로 판매가를
    산출한다. target_margin_rate는 쓰지 않는다(그건 매출액 대비 마진율이라
    이 정책의 "매입가 대비" 정의와 다른 값이 되기 때문).
    쿠팡 수수료(market_fee_rate)와 택배비(outbound_shipping_cost)는 실제로 나가는
    비용이라 이익금 계산과 무관하게 판매가에는 항상 반영된다.
    """
    target_rate = _tiered_target_margin_rate(purchase_cost, tiers)
    profit_by_rate = purchase_cost * target_rate
    profit_target = max(profit_by_rate, minimum_fixed_margin)

    price = calculate_selling_price(
        MarginInputs(
            purchase_cost=purchase_cost,
            market_fee_rate=market_fee_rate,
            source_shipping_cost=outbound_shipping_cost,
            fixed_margin=profit_target,
            target_margin_rate=Decimal("0"),
        )
    )
    return round_up_to_price_unit(price, COUPANG_PRICE_ROUNDING_UNIT)


def calculate_actual_net_profit(price: Decimal, inputs: MarginInputs) -> Decimal:
    """산출된 판매가로 실제 발생하는 순이익을 역산한다 (역마진 검증용).

    net_profit = price*(1 - F_rate - VAT_rate) - P_cost - S_cost + S_charge

    calculate_selling_price로 산출한 price를 넣으면 이론상
    net_profit == fixed_margin + price*target_margin_rate (>= fixed_margin) 가 성립해야 한다.
    """
    market_fee = price * inputs.market_fee_rate
    vat_cost = price * inputs.effective_vat_rate
    return (
        price
        - market_fee
        - vat_cost
        - inputs.purchase_cost
        - inputs.source_shipping_cost
        + inputs.customer_shipping_charge
    )
