"""배경 합성 모듈 (PRD 3.2 2단계).

주의 — 실사이트 미검증: Replicate API(api.replicate.com)는 이 개발 환경에서 아웃바운드
접속이 차단되어 있어 실제 호출을 검증하지 못했다. 아래 구현은 Replicate의 공개 문서에
따른 공식 모델 엔드포인트 패턴(POST /v1/models/{owner}/{name}/predictions, 버전 해시
불필요)을 따르지만, 실제 API 토큰으로 로컬 PC에서 한 번 검증이 필요하다.
"""

import asyncio
import io
import time

import httpx
from PIL import Image, ImageDraw

from app.core.config import Settings
from app.integrations.ai_vision.base import BaseBackgroundSynthesizer

REPLICATE_API_BASE = "https://api.replicate.com/v1"
DEFAULT_BACKGROUND_MODEL = "stability-ai/sdxl"

# PRD 3.2: 제품 카테고리/컬러톤을 반영한 스튜디오 배경 생성 프롬프트.
PROMPT_TEMPLATE = (
    "Minimalist studio product photography background for a {category}, "
    "{color_tone} color palette, clean concrete pedestal, soft natural morning sunlight, "
    "cinematic shadows, 8k resolution, photorealistic, empty background with no objects"
)


class ReplicateBackgroundSynthesizer(BaseBackgroundSynthesizer):
    """Replicate의 텍스트→이미지 모델로 스튜디오 배경을 생성한 뒤, 제품 객체를 합성한다."""

    def __init__(self, settings: Settings, model: str = DEFAULT_BACKGROUND_MODEL, poll_timeout: float = 60.0):
        if not settings.replicate_api_token:
            raise ValueError("REPLICATE_API_TOKEN 환경변수가 설정되어 있지 않습니다.")
        self._token = settings.replicate_api_token
        self._model = model
        self._poll_timeout = poll_timeout

    async def synthesize(self, object_image: bytes, category: str, color_tone: str) -> bytes:
        object_img = Image.open(io.BytesIO(object_image)).convert("RGBA")
        width, height = object_img.size

        prompt = PROMPT_TEMPLATE.format(category=category or "product", color_tone=color_tone or "neutral")
        background_bytes = await self._generate_background(prompt, width, height)

        background_img = Image.open(io.BytesIO(background_bytes)).convert("RGBA").resize((width, height))
        background_img.alpha_composite(object_img)

        buf = io.BytesIO()
        background_img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()

    async def _generate_background(self, prompt: str, width: int, height: int) -> bytes:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        payload = {"input": {"prompt": prompt, "width": width, "height": height}}

        async with httpx.AsyncClient(timeout=30.0) as client:
            create_response = await client.post(
                f"{REPLICATE_API_BASE}/models/{self._model}/predictions", headers=headers, json=payload
            )
            create_response.raise_for_status()
            prediction = create_response.json()

            deadline = time.monotonic() + self._poll_timeout
            while prediction.get("status") not in ("succeeded", "failed", "canceled"):
                if time.monotonic() > deadline:
                    raise TimeoutError("Replicate 배경 생성이 시간 내에 끝나지 않았습니다.")
                await asyncio.sleep(2.0)
                poll_response = await client.get(f"{REPLICATE_API_BASE}/predictions/{prediction['id']}", headers=headers)
                poll_response.raise_for_status()
                prediction = poll_response.json()

            if prediction.get("status") != "succeeded":
                raise RuntimeError(f"Replicate 배경 생성 실패: {prediction.get('error')}")

            output = prediction.get("output")
            image_url = output[0] if isinstance(output, list) else output
            image_response = await client.get(image_url)
            image_response.raise_for_status()
            return image_response.content


class MockBackgroundSynthesizer(BaseBackgroundSynthesizer):
    """실제 AI 호출 없이 로컬 개발/테스트가 가능한 Mock.

    간단한 그라데이션 배경을 로컬에서 생성해 실제로 합성까지 수행한다 —
    "가짜 응답을 반환"하는 게 아니라 진짜 이미지 처리 결과를 돌려준다.
    """

    _COLOR_TONE_PRESETS: dict[str, tuple[int, int, int]] = {
        "white": (245, 245, 245),
        "black": (30, 30, 30),
        "beige": (222, 208, 184),
        "neutral": (235, 235, 230),
    }

    async def synthesize(self, object_image: bytes, category: str, color_tone: str) -> bytes:
        object_img = Image.open(io.BytesIO(object_image)).convert("RGBA")
        width, height = object_img.size

        base_color = self._COLOR_TONE_PRESETS.get((color_tone or "neutral").lower(), (235, 235, 230))
        background = Image.new("RGBA", (width, height), (*base_color, 255))
        # 위에서 아래로 살짝 어두워지는 단순 그라데이션 — 실제 스튜디오 조명 느낌의 최소 근사.
        draw = ImageDraw.Draw(background)
        for y in range(height):
            factor = 1.0 - (y / height) * 0.15
            shade = tuple(min(255, int(c * factor)) for c in base_color)
            draw.line([(0, y), (width, y)], fill=(*shade, 255))

        background.alpha_composite(object_img)

        buf = io.BytesIO()
        background.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
