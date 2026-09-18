from abc import ABC, abstractmethod


class BaseMarketClient(ABC):
    """네이버 커머스 / 쿠팡 Wing / ESM Plus 공통 인터페이스 (PRD 4.2, 9.2).

    구현은 4차 지시(오픈마켓 API 연동 및 마진 엔진)에서 작성한다.
    """

    @abstractmethod
    async def register_product(self, product_id: int, selling_price: float, assets: dict) -> str:
        """상품을 마켓에 등록하고 market_product_id를 반환한다."""
        raise NotImplementedError

    @abstractmethod
    async def set_sold_out(self, market_product_id: str) -> None:
        """PRD 9.2: 소싱처 전체 품절 시 즉시 판매중지 처리."""
        raise NotImplementedError

    @abstractmethod
    async def approve_shipment(self, market_order_id: str, courier_code: str, tracking_no: str) -> None:
        """PRD 6.1: 운송장 등록 후 마켓 발송 처리 API 호출."""
        raise NotImplementedError
