from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ShippingInfo:
    recipient_name: str
    recipient_phone: str
    shipping_addr: str
    shipping_message: str = ""


@dataclass
class TrackingInfo:
    """PRD 6.1 발송처리에 필요한 소싱처(ABC마트) 운송장 정보."""

    courier_name: str
    courier_code: str  # 쿠팡 deliveryCompanyCode 값 (예: "CJGLS")
    tracking_no: str


class BaseRPAClient(ABC):
    """Headless Playwright 기반 소싱처 무인 발주 인터페이스 (PRD 5.2, 6.1).

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

    @abstractmethod
    async def fetch_tracking_info(self, source_order_id: str) -> TrackingInfo | None:
        """소싱처 마이페이지 주문내역에서 택배사명/운송장번호를 조회한다 (PRD 6.1).

        아직 송장이 발급되지 않았으면(소싱처가 아직 발송 준비 중이면) None을 돌려준다 —
        호출부는 이 경우 나중에 다시 폴링해야 한다.
        """
        raise NotImplementedError
