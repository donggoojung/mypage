import io

import pytest
from PIL import Image

from app.integrations.ai_vision.background_synthesizer import MockBackgroundSynthesizer
from app.integrations.ai_vision.segmenter import MockObjectSegmenter


def _sample_png(size=(120, 120), color=(10, 120, 200)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_mock_segmenter_produces_transparent_png_same_size():
    segmenter = MockObjectSegmenter()
    source = _sample_png()

    result = await segmenter.segment(source)

    image = Image.open(io.BytesIO(result))
    assert image.mode == "RGBA"
    assert image.size == (120, 120)
    # 알파 채널이 균일하지 않아야(=일부 투명 처리됨) 실제로 "분할"이 일어난 것이다.
    alpha_min, alpha_max = image.getchannel("A").getextrema()
    assert alpha_min != alpha_max


@pytest.mark.asyncio
async def test_mock_synthesizer_produces_opaque_rgb_image():
    segmenter = MockObjectSegmenter()
    synthesizer = MockBackgroundSynthesizer()
    object_png = await segmenter.segment(_sample_png())

    result = await synthesizer.synthesize(object_png, category="운동화", color_tone="beige")

    image = Image.open(io.BytesIO(result))
    assert image.mode == "RGB"
    assert image.size == (120, 120)


@pytest.mark.asyncio
async def test_mock_synthesizer_falls_back_to_neutral_for_unknown_color_tone():
    segmenter = MockObjectSegmenter()
    synthesizer = MockBackgroundSynthesizer()
    object_png = await segmenter.segment(_sample_png())

    result = await synthesizer.synthesize(object_png, category="가방", color_tone="무지개색")

    # 예외 없이 결과가 나와야 한다 (사전 정의 안 된 색상톤도 안전하게 처리).
    image = Image.open(io.BytesIO(result))
    assert image.size == (120, 120)
