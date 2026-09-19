from functools import lru_cache

from app.core.config import get_settings
from app.integrations.ai_vision.background_synthesizer import (
    MockBackgroundSynthesizer,
    ReplicateBackgroundSynthesizer,
)
from app.integrations.ai_vision.base import BaseBackgroundSynthesizer, BaseObjectSegmenter
from app.integrations.ai_vision.segmenter import MockObjectSegmenter, RembgObjectSegmenter


@lru_cache
def get_object_segmenter() -> BaseObjectSegmenter:
    settings = get_settings()
    if settings.use_mock_ai_vision:
        return MockObjectSegmenter()
    return RembgObjectSegmenter()


@lru_cache
def get_background_synthesizer() -> BaseBackgroundSynthesizer:
    settings = get_settings()
    if settings.use_mock_ai_vision:
        return MockBackgroundSynthesizer()
    return ReplicateBackgroundSynthesizer(settings)
