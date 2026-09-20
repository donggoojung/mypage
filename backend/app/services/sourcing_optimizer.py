"""신규 주문 유입 시 복수 소싱처 중 최저 결제 예정가를 판별한다 (PRD 5.1).

절차:
1. 마스터 상품에 등록된 소싱처(source_mappings) 후보 목록을 조회한다.
2. 각 후보의 실시간 재고/가격을 다시 스크래핑해 최신 상태로 갱신한다
   (스크래퍼가 아직 구현되지 않은 소싱처는 DB에 저장된 최신 스냅샷으로 대체한다).
3. 주문된 사이즈의 재고가 있는 후보만 남기고, 소싱처별 쿠폰/캐시 적립률을
   반영한 "결제 예정가"를 계산해 가장 저렴한 1곳을 최종 매입처로 확정한다.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.scrapers.factory import get_scraper
from app.models.enums import SourcePlatform
from app.models.source_mapping import SourceMapping

# PRD 5.1 "각 공급처의 현재 결제 예정가(자체 쿠폰 적용가)를 계산한다" —
# 소싱처별 자체 쿠폰/적립금 등으로 실결제가가 표시가보다 낮아지는 비율.
# 실제 값은 소싱처 프로모션에 따라 계속 바뀌므로, 운영 중 주기적으로 갱신해야 한다.
PLATFORM_COUPON_RATES: dict[SourcePlatform, Decimal] = {
    SourcePlatform.ABC_MART: Decimal("0"),
    SourcePlatform.MUSINSA: Decimal("0.03"),
    SourcePlatform.FOLDER: Decimal("0.05"),
    SourcePlatform.BRAND_OFFICIAL: Decimal("0"),
    SourcePlatform.OTHER: Decimal("0"),
}


class NoAvailableSourceError(Exception):
    """주문된 사이즈의 재고를 보유한 소싱처가 하나도 없을 때 발생한다."""


@dataclass
class SourcingQuote:
    source_mapping_id: int
    source_platform: SourcePlatform
    source_url: str
    style_code: str
    size: str
    listed_price: Decimal
    coupon_rate: Decimal
    expected_payment: Decimal
    stock: int


def _quantize(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


async def _refresh_mapping(mapping: SourceMapping, style_code: str) -> tuple[Decimal, dict]:
    """가능하면 실시간 스크래핑으로 가격/재고를 갱신하고, 안 되면 DB 스냅샷을 그대로 쓴다."""
    try:
        scraper = get_scraper(mapping.source_platform)
        product = await scraper.fetch_product(style_code)
        return Decimal(str(product.price)), product.size_stock
    except NotImplementedError:
        return Decimal(str(mapping.source_price)), mapping.size_stock_json or {}


async def find_cheapest_source(
    session: Session,
    product_id: int,
    size: str,
    quantity: int = 1,
) -> SourcingQuote:
    """등록된 소싱처 중 주문된 사이즈의 재고가 있는 곳들의 결제 예정가를 비교해 최저가 1곳을 반환한다."""
    mappings = session.scalars(select(SourceMapping).where(SourceMapping.product_id == product_id)).all()
    if not mappings:
        raise NoAvailableSourceError(f"product_id={product_id} 에 등록된 소싱처가 없습니다.")

    style_code = mappings[0].product.style_code

    quotes: list[SourcingQuote] = []
    for mapping in mappings:
        listed_price, size_stock = await _refresh_mapping(mapping, style_code)

        size_info = size_stock.get(size)
        if not size_info or size_info.get("is_sold_out", True):
            continue
        stock = int(size_info.get("stock", 0))
        if stock < quantity:
            continue

        coupon_rate = PLATFORM_COUPON_RATES.get(mapping.source_platform, Decimal("0"))
        expected_payment = _quantize(listed_price * (Decimal("1") - coupon_rate)) * quantity

        quotes.append(
            SourcingQuote(
                source_mapping_id=mapping.source_id,
                source_platform=mapping.source_platform,
                source_url=mapping.source_url,
                style_code=style_code,
                size=size,
                listed_price=listed_price,
                coupon_rate=coupon_rate,
                expected_payment=expected_payment,
                stock=stock,
            )
        )

        # 다음 조회를 위해 최신 스냅샷을 DB에도 반영해둔다 (감사 추적용).
        mapping.source_price = float(listed_price)
        mapping.size_stock_json = size_stock
        mapping.last_checked_at = datetime.now(UTC)

    session.commit()

    if not quotes:
        raise NoAvailableSourceError(
            f"product_id={product_id}, size={size} 재고를 보유한 소싱처가 없습니다 (1순위 최저가 플랫폼 확정 불가)."
        )

    return min(quotes, key=lambda q: q.expected_payment)
