"""경쟁가 대비 판매가 조정 로직.

역마진 방지 수식(margin_engine)으로 산출한 "계산가"는 원가 기준으로 목표마진을
보장하지만, 실제 시장(네이버쇼핑/쿠팡)에 그보다 싼 경쟁 상품이 있으면 판매가 저조로
이어질 수 있다. 이 모듈은 경쟁가를 반영해 판매가를 낮출지 판단하되, 상품 1건당
반드시 지켜야 하는 고정 마진(M_fixed)만큼은 절대 깎이지 않도록 보장한다.
"""

from dataclasses import dataclass, replace
from decimal import Decimal

from app.services.margin_engine import MarginInputs, calculate_actual_net_profit, calculate_selling_price


@dataclass(frozen=True)
class PriceRecommendation:
    engine_price: Decimal
    """원가 기반 역마진 방지 수식으로만 산출한 가격 (경쟁가 미반영)."""
    competitor_price: Decimal | None
    """네이버쇼핑/쿠팡 등에서 조회한 동일·유사 상품 최저가. 조회 실패 시 None."""
    minimum_safe_price: Decimal
    """목표 변동마진율을 0으로 두고 고정마진만 지켰을 때의 최저 마지노선 가격."""
    recommended_price: Decimal
    """실제 등록을 권장하는 최종 가격."""
    matched_competitor: bool
    """경쟁가에 맞춰(또는 그 이하로) 조정했는지 여부."""
    note: str


def recommend_price(margin_inputs: MarginInputs, competitor_price: Decimal | None) -> PriceRecommendation:
    """경쟁가를 고려해 최종 판매가를 추천한다. 고정마진 미만으로는 절대 내려가지 않는다."""
    engine_price = calculate_selling_price(margin_inputs)
    # 목표 변동마진율만 0으로 낮춘 "고정마진만 지키는 최저가" — 이 아래로는 절대 내려가면 안 된다.
    floor_inputs = replace(margin_inputs, target_margin_rate=Decimal("0"))
    minimum_safe_price = calculate_selling_price(floor_inputs)

    if competitor_price is None:
        return PriceRecommendation(
            engine_price=engine_price,
            competitor_price=None,
            minimum_safe_price=minimum_safe_price,
            recommended_price=engine_price,
            matched_competitor=False,
            note="경쟁가 정보를 가져오지 못해 원가 기반 계산가를 그대로 사용합니다.",
        )

    if engine_price <= competitor_price:
        return PriceRecommendation(
            engine_price=engine_price,
            competitor_price=competitor_price,
            minimum_safe_price=minimum_safe_price,
            recommended_price=engine_price,
            matched_competitor=False,
            note="계산가가 이미 경쟁 최저가보다 낮거나 같아 그대로 사용합니다.",
        )

    if competitor_price >= minimum_safe_price:
        # 경쟁가에 맞춰도 고정마진(M_fixed)은 여전히 지켜지므로 안전하게 조정 가능.
        actual_profit = calculate_actual_net_profit(competitor_price, margin_inputs)
        return PriceRecommendation(
            engine_price=engine_price,
            competitor_price=competitor_price,
            minimum_safe_price=minimum_safe_price,
            recommended_price=competitor_price,
            matched_competitor=True,
            note=(
                f"경쟁 최저가({competitor_price:,}원)에 맞춰도 실제 마진이 "
                f"{actual_profit:,.0f}원으로 고정마진 이상 유지되어 경쟁가로 조정합니다."
            ),
        )

    return PriceRecommendation(
        engine_price=engine_price,
        competitor_price=competitor_price,
        minimum_safe_price=minimum_safe_price,
        recommended_price=engine_price,
        matched_competitor=False,
        note=(
            f"경쟁 최저가({competitor_price:,}원)를 따라가면 고정마진 미만(역마진 위험)이 되어 "
            f"계산가({engine_price:,}원)를 유지합니다. (마지노선: {minimum_safe_price:,}원)"
        ),
    )
