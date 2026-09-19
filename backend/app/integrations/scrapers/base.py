from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ScrapedProduct:
    """소싱처 스크래핑 결과 — PRD 2.1 정규화 수집 데이터 (품번 기준)."""

    style_code: str
    brand_name: str
    product_name: str
    source_url: str
    price: float
    image_url: str = ""
    # 예: {"250": {"stock": 3, "is_sold_out": False}, "260": {"stock": 0, "is_sold_out": True}}
    size_stock: dict[str, dict] = field(default_factory=dict)
    raw_specs: dict = field(default_factory=dict)


class BaseScraper(ABC):
    """소싱처(ABC마트, 무신사, 폴더 등) 스크래퍼 공통 인터페이스.

    실제 구현(XHR 역공학 + Playwright + 프록시 로테이션, PRD 2.2)은 2차 지시에서 작성한다.
    """

    @abstractmethod
    async def fetch_product(self, style_code: str) -> ScrapedProduct:
        """품번(Style Code)으로 소싱처 상품 상세(가격/사이즈별 재고)를 조회한다."""
        raise NotImplementedError
