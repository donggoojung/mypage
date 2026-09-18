from abc import ABC, abstractmethod


class BaseAIVisionClient(ABC):
    """Rembg/SAM 객체 분할 + 생성형 배경 합성 인터페이스 (PRD 3.2).

    구현은 3차 지시(AI 이미지 생성 및 상세페이지 빌더)에서 작성한다.
    """

    @abstractmethod
    async def segment_object(self, source_image_url: str) -> bytes:
        """제품 객체만 남기고 모델/배경을 제거한 알파 매트 PNG를 반환한다."""
        raise NotImplementedError

    @abstractmethod
    async def synthesize_background(self, object_image: bytes, category: str, color_tone: str) -> bytes:
        """분할된 제품 객체에 스튜디오 배경을 생성 합성한 최종 이미지를 반환한다."""
        raise NotImplementedError
