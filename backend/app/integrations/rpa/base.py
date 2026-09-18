from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ShippingInfo:
    recipient_name: str
    recipient_phone: str
    shipping_addr: str
    shipping_message: str = ""


class BaseRPAClient(ABC):
    """Headless Playwright 기반 소싱처 무인 발주 인터페이스 (PRD 5.2).

    구현은 5차 지시(주문 감지, 최저가 판별, 무인 RPA 발주)에서 작성한다.
    """

    @abstractmethod
    async def purchase_order(self, style_code: str, size: str, shipping_info: ShippingInfo) -> str:
        """소싱처 사이트에서 무인 결제를 완료하고 소싱처 주문번호를 반환한다."""
        raise NotImplementedError
