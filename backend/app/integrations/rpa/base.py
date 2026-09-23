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
    """PRD 6.1 발송처리에 필요한 소싱처 운송장 정보."""

    courier_name: str
    courier_code: str  # 쿠팡 deliveryCompanyCode 값 (예: "CJGLS")
    tracking_no: str


# 최종 결제 직전 단계까지만 진행하고 멈췄을 때 반환하는 값 — 실제 주문번호가 아니라는 걸
# 호출자가 바로 알아볼 수 있게 접두어를 확실히 다르게 둔다. 소싱처(ABC마트/무신사 등)에
# 상관없이 order_processor.py가 공통으로 검사하는 값이라 여기(공통 base)에 둔다.
REVIEW_ONLY_PREFIX = "REVIEW_ONLY"


class RPAPurchaseError(RuntimeError):
    """무인 발주 진행 중(재고 소진, 세션 만료, 결제 실패 등) 발생한 오류. 소싱처 공통."""


def validate_shipping_info(shipping_info: ShippingInfo) -> None:
    missing = [
        field
        for field in ("recipient_name", "recipient_phone", "shipping_addr")
        if not getattr(shipping_info, field)
    ]
    if missing:
        raise RPAPurchaseError(f"배송지 정보 누락: {missing}")


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
