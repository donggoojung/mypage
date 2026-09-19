from abc import ABC, abstractmethod


class BaseObjectSegmenter(ABC):
    """제품 이미지에서 모델/배경을 제거하고 순수 객체만 남기는 인터페이스 (PRD 3.2 1단계)."""

    @abstractmethod
    async def segment(self, image_bytes: bytes) -> bytes:
        """원본 이미지 바이트를 받아, 배경이 투명 처리된 알파 매트 PNG 바이트를 반환한다."""
        raise NotImplementedError


class BaseBackgroundSynthesizer(ABC):
    """분할된 제품 객체에 신규 스튜디오 배경을 생성 합성하는 인터페이스 (PRD 3.2 2단계)."""

    @abstractmethod
    async def synthesize(self, object_image: bytes, category: str, color_tone: str) -> bytes:
        """배경이 투명한 제품 이미지를 받아, 새 배경이 합성된 최종 이미지(PNG bytes)를 반환한다."""
        raise NotImplementedError
