"""구글 AI 스튜디오(Gemini) 연동 클라이언트.

크롤링된 소싱처(ABC마트 등) 원본 상품 데이터를 오픈마켓(네이버/쿠팡) 등록용으로 가공한다:
- 난잡한 원본 상품명 → SEO 최적화된 상품명
- 상세페이지 원문 텍스트 → 상품정보제공고시용 정형 JSON(소재/색상/굽높이 등)

google-genai(https://github.com/googleapis/python-genai) SDK를 쓴다. GEMINI_API_KEY가
없거나 use_mock=True면 규칙 기반 Mock으로 동작해, 실제 키 없이도 로직 검증이 가능하다
(다른 실연동 클라이언트인 CoupangWingClient와 동일한 Mock/Real 겸용 패턴).
"""

import json
import re

from app.core.config import Settings, get_settings

# "소재: 천연가죽" 같은 "키: 값" 한 줄짜리 스펙 라인을 찾는 패턴 (Mock 모드에서 사용).
_KEY_VALUE_LINE_PATTERN = re.compile(r"^\s*([가-힣A-Za-z0-9()/·\s]+?)\s*[:：]\s*(.+?)\s*$")

# 상품정보제공고시에서 보통 요구하는 신발류 필수 항목 — 원문에 없으면 이 기본값으로 채운다.
REQUIRED_SPEC_KEYS = ("소재", "색상", "굽높이", "제조자(수입자)", "제조국", "취급시 주의사항")

MAX_SEO_TITLE_LENGTH = 50


class GeminiClientError(RuntimeError):
    """Gemini 호출 또는 응답 파싱이 실패했을 때 발생한다."""


class GeminiClient:
    """Mock/Real 겸용 Gemini 클라이언트."""

    def __init__(self, settings: Settings | None = None, use_mock: bool | None = None):
        self._settings = settings or get_settings()
        self._use_mock = (
            (self._settings.use_mock_llm or not self._settings.gemini_api_key)
            if use_mock is None
            else use_mock
        )
        self._sdk_client = None
        if not self._use_mock:
            try:
                from google import genai
            except ImportError as exc:
                raise GeminiClientError(
                    "google-genai 패키지가 설치되어 있지 않습니다. `pip install google-genai`로 설치하세요."
                ) from exc
            self._sdk_client = genai.Client(api_key=self._settings.gemini_api_key)

    # --- 1) 상품명 SEO 정제 ---------------------------------------------------

    async def generate_seo_title(self, brand: str, raw_title: str, style_code: str, category: str) -> str:
        """소싱처의 난잡한 상품명을 "[브랜드명] 모델명 품번 핵심키워드" 형태(50자 내외)로 정제한다."""
        if self._use_mock:
            return _mock_generate_seo_title(brand, raw_title, style_code, category)
        return await self._real_generate_seo_title(brand, raw_title, style_code, category)

    async def _real_generate_seo_title(self, brand: str, raw_title: str, style_code: str, category: str) -> str:
        from google.genai import types

        prompt = (
            "너는 한국 오픈마켓(네이버 스마트스토어/쿠팡)의 SEO 상품명 전문가다. "
            "아래 원본 정보를 보고, 검색 노출에 최적화된 상품명을 딱 1줄로만 만들어라.\n\n"
            f"브랜드: {brand}\n원본 상품명: {raw_title}\n품번: {style_code}\n카테고리: {category}\n\n"
            "규칙:\n"
            "- 형식: [브랜드명] 모델명 품번 핵심키워드\n"
            "- 전체 길이는 공백 포함 50자 이내\n"
            "- 브랜드명을 두 번 반복하지 마라\n"
            "- 특수문자, 이모지, 과장 문구(최저가/공짜 등)를 쓰지 마라\n"
            "- 설명 없이 상품명 한 줄만 출력해라"
        )
        try:
            response = await self._sdk_client.aio.models.generate_content(
                model=self._settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=100),
            )
        except Exception as exc:  # noqa: BLE001 - SDK 예외 유형이 다양해 공통 래핑한다.
            raise GeminiClientError(f"Gemini SEO 상품명 생성 실패: {exc}") from exc

        title = (response.text or "").strip().strip('"').splitlines()[0].strip()
        if not title:
            raise GeminiClientError(f"Gemini가 빈 응답을 반환했습니다: {response}")
        return title[:MAX_SEO_TITLE_LENGTH]

    # --- 2) 상세 스펙 요약 -------------------------------------------------

    async def summarize_product_specs(self, raw_description_text: str) -> dict:
        """상세페이지 원문 텍스트를 소재/색상/굽높이 등 상품정보제공고시용 정형 JSON으로 추출한다."""
        if self._use_mock:
            return _mock_summarize_product_specs(raw_description_text)
        return await self._real_summarize_product_specs(raw_description_text)

    async def _real_summarize_product_specs(self, raw_description_text: str) -> dict:
        from google.genai import types

        prompt = (
            "너는 한국 오픈마켓 '상품정보제공고시' 담당자다. 아래 원문에서 다음 항목을 찾아 "
            "JSON 객체 하나로만 답해라(설명 문장 없이 JSON만): "
            f"{', '.join(REQUIRED_SPEC_KEYS)}. "
            '원문에 없는 항목은 값을 "상품 상세 참조"로 채워라.\n\n'
            f"원문:\n{raw_description_text}"
        )
        try:
            response = await self._sdk_client.aio.models.generate_content(
                model=self._settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.1, response_mime_type="application/json"),
            )
        except Exception as exc:  # noqa: BLE001
            raise GeminiClientError(f"Gemini 스펙 요약 실패: {exc}") from exc

        try:
            parsed = json.loads(response.text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise GeminiClientError(f"Gemini 응답이 유효한 JSON이 아닙니다: {response.text!r}") from exc

        if not isinstance(parsed, dict):
            raise GeminiClientError(f"Gemini 응답이 JSON 객체가 아닙니다: {parsed!r}")
        return parsed


# --- Mock 구현 (규칙 기반) --------------------------------------------------


def _mock_generate_seo_title(brand: str, raw_title: str, style_code: str, category: str) -> str:
    brand = (brand or "").strip()
    title = (raw_title or "").strip()
    # 원본 상품명이 브랜드로 시작하면(소싱처 데이터에 흔함, 심지어 "나이키 나이키 ..."처럼
    # 중복인 경우도 있음) 앞쪽에 반복된 브랜드 표기를 전부 제거한다.
    while brand and title.startswith(brand):
        title = title[len(brand) :].strip()

    parts = [f"[{brand}]" if brand else "", title, style_code or "", category or ""]
    result = " ".join(p.strip() for p in parts if p and p.strip())
    return result[:MAX_SEO_TITLE_LENGTH].strip()


def _mock_summarize_product_specs(raw_description_text: str) -> dict:
    extracted: dict[str, str] = {}
    for line in (raw_description_text or "").splitlines():
        match = _KEY_VALUE_LINE_PATTERN.match(line)
        if not match:
            continue
        key, value = match.group(1).strip(), match.group(2).strip()
        if key and value:
            extracted[key] = value

    result = dict(extracted)
    for key in REQUIRED_SPEC_KEYS:
        result.setdefault(key, "상품 상세 참조")
    return result
