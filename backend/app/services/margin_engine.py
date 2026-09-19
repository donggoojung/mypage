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
