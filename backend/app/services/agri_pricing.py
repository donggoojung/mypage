"""농산물 위탁판매 가격 계산과 상품 표시 문구.

가격 원칙
- 원가 = 공급가 + 택배비 (공급가에 택배비가 포함돼 있으면 택배비 0)
- 판매가 = (원가 + 최소 이익) ÷ (1 - 마켓 수수료율), 100원 단위 올림
- 가공하지 않은 농산물은 부가세 면세라 판매가에 부가세를 더하지 않는다.
  마켓 수수료에는 부가세가 붙으므로 수수료율은 부가세 포함 값으로 넣는다.
"""

from __future__ import annotations

import math
from copy import deepcopy

MARKETS: dict[str, str] = {
    "smartstore": "스마트스토어",
    "coupang": "쿠팡",
    "toss": "토스쇼핑",
    "11st": "11번가",
    "gmarket": "G마켓·옥션",
}

CATEGORIES: dict[str, str] = {
    "fruit": "과일",
    "vegetable": "채소",
    "rice": "쌀",
    "grain": "잡곡",
    "etc": "기타",
}

ORDER_STATUSES: dict[str, str] = {
    "new": "발주 대기",
    "ordered": "발주 완료",
    "invoiced": "송장 받음",
    "shipped": "마켓 송장 입력 끝",
    "cancelled": "취소",
}

PRODUCT_STATUSES: dict[str, str] = {
    "ready": "판매 준비",
    "selling": "판매중",
    "soldout": "품절",
    "stopped": "판매 중지",
}

ORDER_METHODS: dict[str, str] = {
    "excel": "엑셀 발주",
    "auto": "자동발주",
    "kakao": "카카오톡",
    "etc": "기타",
}

# 수수료율(%)은 부가세 포함 대략값이다. 실제 값은 각 마켓 판매자센터에서 확인 후 설정 화면에서 고친다.
DEFAULT_SETTINGS: dict = {
    "fee_rates": {
        "smartstore": 6.4,
        "coupang": 11.7,
        "toss": 8.8,
        "11st": 13.0,
        "gmarket": 13.0,
    },
    # 과일 4,000원은 확정. 나머지는 대표님이 정하기 전까지 쓰는 임시값.
    "min_profit": {
        "fruit": 4000,
        "vegetable": 3000,
        "rice": 3000,
        "grain": 3000,
        "etc": 3000,
    },
    "price_unit": 100,
    "cs_phone": "",
    "claim_hours": 24,
}

SETTINGS_KEY = "general"


def merge_settings(stored: dict | None) -> dict:
    """저장된 설정에 빠진 항목은 기본값으로 채운다 (마켓·품목이 나중에 늘어나도 깨지지 않게)."""
    merged = deepcopy(DEFAULT_SETTINGS)
    if not stored:
        return merged
    for key, value in stored.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def unit_cost(supply_price: int, shipping_fee: int) -> int:
    return int(supply_price) + int(shipping_fee)


def recommended_price(cost: int, min_profit: int, fee_rate_percent: float, price_unit: int = 100) -> int:
    rate = float(fee_rate_percent) / 100
    if rate >= 1:
        raise ValueError("수수료율이 100% 이상이라 가격을 계산할 수 없습니다.")
    raw = (cost + min_profit) / (1 - rate)
    unit = max(int(price_unit), 1)
    # 부동소수 오차로 정확히 나눠떨어지는 값이 한 단위 올라가지 않도록 아주 작은 값을 뺀다.
    return int(math.ceil(raw / unit - 1e-9) * unit)


def profit_of(sale_price: int, cost: int, fee_rate_percent: float) -> dict:
    fee = int(round(sale_price * float(fee_rate_percent) / 100))
    return {"fee": fee, "profit": sale_price - fee - cost}


def price_table(
    supply_price: int,
    shipping_fee: int,
    category: str,
    settings: dict,
    sale_price: int | None = None,
) -> list[dict]:
    """마켓마다 추천 판매가와 예상 이익을 계산한다.

    sale_price를 주면(대표님이 직접 정한 가격) 그 가격 기준 이익을 보여주고,
    최소 이익에 못 미치면 below_min으로 경고한다.
    """
    cost = unit_cost(supply_price, shipping_fee)
    min_profit = int(settings["min_profit"].get(category, settings["min_profit"].get("etc", 0)))
    rows = []
    for market, label in MARKETS.items():
        rate = float(settings["fee_rates"].get(market, 0))
        recommended = recommended_price(cost, min_profit, rate, settings.get("price_unit", 100))
        price = int(sale_price) if sale_price else recommended
        money = profit_of(price, cost, rate)
        rows.append(
            {
                "market": market,
                "market_label": label,
                "fee_rate": rate,
                "recommended_price": recommended,
                "price": price,
                "fee": money["fee"],
                "profit": money["profit"],
                "below_min": money["profit"] < min_profit,
            }
        )
    return rows


def order_profit(sale_amount: int, unit_cost_value: int, quantity: int, fee_rate_percent: float) -> int:
    fee = int(round(sale_amount * float(fee_rate_percent) / 100))
    return sale_amount - fee - unit_cost_value * quantity


def _line(title: str, value: str | None) -> str:
    return f"- {title}: {value if value else '(입력 필요)'}"


def product_notice_text(product, settings: dict) -> str:
    """상세페이지 아래에 붙일 상품정보제공고시, 원산지, 반품 안내 문구."""
    option_lines = []
    for option in product.options:
        parts = [p for p in (option.weight, option.count, option.grade) if p]
        option_lines.append(f"{option.label}" + (f" ({', '.join(parts)})" if parts else ""))
    options_text = " / ".join(option_lines) if option_lines else None

    lines = ["[상품정보제공고시 - 농수산물]"]
    lines.append(_line("품목 또는 명칭", product.name))
    lines.append(_line("포장 단위별 중량, 수량, 크기", options_text))
    lines.append(_line("생산자", product.producer))
    lines.append(_line("원산지", product.origin))
    lines.append(_line("포장일 또는 생산연도", product.packing_info or "주문 후 산지에서 포장하여 발송"))
    lines.append(_line("보관 방법", product.storage))
    if product.category == "rice":
        lines.append("- 관련 법상 표시사항: 양곡관리법에 따른 양곡 표시사항 (아래 참고)")
    else:
        lines.append("- 관련 법상 표시사항: 해당 없음")
    lines.append(_line("소비자 상담 전화번호", settings.get("cs_phone")))

    if product.category == "rice":
        lines.append("")
        lines.append("[양곡 표시사항]")
        lines.append(_line("품목", product.name))
        lines.append(_line("중량", options_text))
        lines.append(_line("생산자", product.producer))
        lines.append(_line("원산지", product.origin))
        lines.append(_line("품종", product.variety))
        lines.append(_line("생산연도", product.harvest_year))
        lines.append("- 도정일자: 포장지에 따로 표시")
        lines.append(_line("등급", product.grade))

    lines.append("")
    lines.append("[원산지]")
    lines.append(f"{product.name}: {product.origin or '(입력 필요)'}")

    hours = settings.get("claim_hours") or 24
    lines.append("")
    lines.append("[교환·반품 안내]")
    lines.append(
        "신선식품 특성상 단순 변심에 의한 교환·반품은 어렵습니다 (전자상거래법 제17조 제2항)."
    )
    lines.append(
        f"상품이 상했거나 파손된 경우, 받으신 뒤 {hours}시간 안에 상품 사진과 함께 문의해 주시면 "
        "확인 후 다시 보내드리거나 환불해 드립니다."
    )
    lines.append("받는 분이 안 계셔서 배송이 늦어지거나 상한 경우에는 교환·환불이 어려울 수 있습니다.")
    return "\n".join(lines)
