"""제품 객체 분할(배경 제거) 모듈 (PRD 3.2 1단계).

rembg(U2Net)는 완전히 로컬에서 동작하는 오픈소스 배경 제거 엔진이라 외부 API 호출이나
네트워크 연결(최초 모델 다운로드 제외) 없이 동작한다. 모델 파일은 처음 실행 시 자동으로
다운로드되어 로컬에 캐시된다.
"""

import asyncio
import io

from PIL import Image, ImageDraw
from rembg import new_session, remove

from app.integrations.ai_vision.base import BaseObjectSegmenter


class RembgObjectSegmenter(BaseObjectSegmenter):
    """rembg(U2Net) 기반 실제 배경 제거."""

    def __init__(self, model_name: str = "u2net"):
        # 세션을 인스턴스당 한 번만 만들어 재사용한다 (모델 로딩 비용 절감).
        self._session = new_session(model_name)

    async def segment(self, image_bytes: bytes) -> bytes:
        # rembg.remove()는 동기(CPU-bound) 함수라 이벤트 루프를 막지 않도록 스레드로 돌린다.
        return await asyncio.to_thread(remove, image_bytes, session=self._session)


class MockObjectSegmenter(BaseObjectSegmenter):
    """실제 모델 없이 로컬 개발/테스트가 가능한 Mock.

    이미지 중앙 80% 영역(타원)만 남기고 나머지를 투명 처리해, "객체만 분리된 것처럼"
    보이는 결과를 빠르게 만들어낸다. 진짜 배경 제거는 아니지만, 파이프라인의 다음 단계
    (배경 합성)를 개발/테스트하기에는 충분하다.
    """

    async def segment(self, image_bytes: bytes) -> bytes:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        width, height = image.size
        margin_x, margin_y = int(width * 0.1), int(height * 0.1)

        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).ellipse([margin_x, margin_y, width - margin_x, height - margin_y], fill=255)
        image.putalpha(mask)

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
