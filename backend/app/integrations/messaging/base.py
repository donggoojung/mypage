from abc import ABC, abstractmethod


class BaseMessagingClient(ABC):
    """솔라피(Solapi) 카카오 알림톡 + SMS/LMS 자동 폴백 인터페이스 (PRD 6.2).

    구현은 6차 지시(배송 추적, 솔라피 알림톡 및 반품 관리)에서 작성한다.
    """

    @abstractmethod
    async def send_kakao_alert(self, phone: str, template_id: str, variables: dict) -> bool:
        """카카오 알림톡을 발송하고, 실패 시 자동으로 SMS/LMS로 대체 발송한다."""
        raise NotImplementedError
