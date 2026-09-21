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
    async def purchase_order(
        self, style_code: str, size: str, shipping_info: ShippingInfo, confirm_final_payment: bool = False
    ) -> str:
        """소싱처 사이트에서 무인 결제를 완료하고 소싱처 주문번호를 반환한다.

        `confirm_final_payment=False`(기본값, 안전)면 결제 직전(최종 주문서 확인) 단계까지만
        진행하고 실제 결제 버튼은 누르지 않는다 — 쿠팡 등록의 `request_approval`과 같은 안전장치다.
        실제로 발주를 완료하려면 이 값을 명시적으로 True로 넘겨야 한다.
        """
        raise NotImplementedError
