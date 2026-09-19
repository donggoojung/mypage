#!/usr/bin/env python
"""ABC마트 상품 URL 1개를 크롤링하고, 마진 엔진으로 플랫폼별 판매가를 계산해 콘솔에 출력한다.

사용법 (backend/ 디렉토리에서 실행):
    python scripts/test_scraper_margin.py <ABC마트 상품 상세 URL>
    python scripts/test_scraper_margin.py <URL> --headless
    python scripts/test_scraper_margin.py <URL> --fixed-margin 7000 --target-margin-rate 0.2

가정: 크롤링된 가격을 그대로 매입원가(P_cost)로 취급한다. 실제로는 회원 등급 할인/
장바구니 쿠폰이 추가로 반영돼야 하지만, 그 로직은 이후 단계(4차 지시)에서 다룬다.
"""

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.scrapers.abc_mart import ABCMartScraper  # noqa: E402
from app.services.margin_engine import (  # noqa: E402
    PLATFORM_DEFAULT_FEE_RATES,
    ReverseMarginError,
    calculate_selling_price_for_platform,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="ABC마트 상품 상세 페이지 URL (또는 로컬 테스트용 file:// URL)")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="브라우저 창을 띄우지 않고 실행한다. 기본은 창을 띄워 눈으로 확인하는 모드다.",
    )
    parser.add_argument("--fixed-margin", type=Decimal, default=Decimal("5000"), help="고정 마진 (기본 5000원)")
    parser.add_argument(
        "--target-margin-rate", type=Decimal, default=Decimal("0.30"), help="목표 변동 마진율 (기본 0.30 = 30%%)"
    )
    parser.add_argument(
        "--customer-shipping-charge", type=Decimal, default=Decimal("3000"), help="고객 청구 배송비 (기본 3000원)"
    )
    parser.add_argument(
        "--source-shipping-cost",
        type=Decimal,
        default=Decimal("0"),
        help="소싱처 부과 배송비 (기본 0원 — 무료배송 가정)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    print(f"[1/2] ABC마트 상품 페이지 크롤링 중: {args.url}")
    scraper = ABCMartScraper(headless=args.headless)
    try:
        product = await scraper.fetch_product_by_url(args.url)
    except Exception as exc:
        print(f"\n크롤링 실패: {exc}")
        print("→ app/integrations/scrapers/abc_mart.py 의 SELECTOR_* 상수가 실사이트와 다를 수 있습니다.")
        print("  브라우저 개발자도구(F12)로 실제 클래스명을 확인해 조정해주세요.")
        raise SystemExit(1)

    print("\n=== 크롤링 결과 ===")
    print(f"브랜드      : {product.brand_name or '(추출 실패)'}")
    print(f"상품명      : {product.product_name or '(추출 실패)'}")
    print(f"품번        : {product.style_code or '(추출 실패)'}")
    print(f"원가/할인가 : {product.price:,.0f}원")
    if product.size_stock:
        print("사이즈별 재고:")
        for size, info in product.size_stock.items():
            status = "품절" if info.get("is_sold_out") else "구매가능"
            print(f"  - {size}: {status}")
    else:
        print("사이즈별 재고: (추출 실패 — SELECTOR_SIZE_OPTIONS 확인 필요)")

    if product.price <= 0:
        print("\n가격을 추출하지 못해 마진 계산을 건너뜁니다.")
        raise SystemExit(1)

    print("\n[2/2] 플랫폼별 판매가 계산 (역마진 방지 수식)")
    print("=== 마진 엔진 계산 결과 ===")
    purchase_cost = Decimal(str(product.price))
    for market_type, fee_rate in PLATFORM_DEFAULT_FEE_RATES.items():
        try:
            price = calculate_selling_price_for_platform(
                market_type=market_type,
                purchase_cost=purchase_cost,
                fixed_margin=args.fixed_margin,
                source_shipping_cost=args.source_shipping_cost,
                customer_shipping_charge=args.customer_shipping_charge,
                target_margin_rate=args.target_margin_rate,
            )
        except ReverseMarginError as exc:
            print(f"{market_type.value:>18}: 계산 불가 ({exc})")
            continue
        print(
            f"{market_type.value:>18}: {price:>10,}원  "
            f"(수수료 {fee_rate:.2%}, 목표마진율 {args.target_margin_rate:.0%})"
        )


if __name__ == "__main__":
    asyncio.run(main())
