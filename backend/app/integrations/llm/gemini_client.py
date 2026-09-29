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

# 쿠팡 검색태그 제한(실API 미검증 — 판매자센터 안내 기준): 최대 20개, 태그당 20자 이내.
MAX_SEARCH_TAGS = 20
MAX_SEARCH_TAG_LENGTH = 20
# AI가 규칙을 어기고 넣을 수 있는 홍보성 문구 — 쿠팡이 검색어 남용으로 볼 수 있어 걸러낸다.
_BANNED_TAG_WORDS = ("최저가", "무료배송", "정품", "특가", "할인", "사은품", "당일발송")
_ALLOWED_TAG_CHARS = re.compile(r"[^가-힣A-Za-z0-9 ]")


def clean_search_tags(tags: list[str]) -> list[str]:
    """특수문자/홍보문구/중복을 걸러내고 쿠팡 제한(개수·길이)에 맞춘다."""
    cleaned: list[str] = []
    for tag in tags:
        tag = _ALLOWED_TAG_CHARS.sub("", tag or "").strip()[:MAX_SEARCH_TAG_LENGTH].strip()
        if not tag or any(word in tag for word in _BANNED_TAG_WORDS) or tag in cleaned:
            continue
        cleaned.append(tag)
    return cleaned[:MAX_SEARCH_TAGS]


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
                # 2026-09-22 실API 검증됨: max_output_tokens를 너무 작게(100) 주면 최신
                # "thinking" 모델이 내부 추론에 토큰을 다 써버려 실제 출력 텍스트가 빈
                # 값으로 온다(IndexError로 이어짐) — 여유 있게 잡고 thinking은 꺼둔다.
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=512,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - SDK 예외 유형이 다양해 공통 래핑한다.
            raise GeminiClientError(f"Gemini SEO 상품명 생성 실패: {exc}") from exc

        raw_text = (response.text or "").strip()
        if not raw_text:
            raise GeminiClientError(
                f"Gemini가 빈 텍스트를 반환했습니다 (finish_reason 등 원인은 raw 응답 참고): {response}"
            )
        title = raw_text.strip('"').splitlines()[0].strip()
        if not title:
            raise GeminiClientError(f"Gemini 응답에서 제목을 추출하지 못했습니다: {raw_text!r}")
        return title[:MAX_SEO_TITLE_LENGTH]

    # --- 1-2) 검색태그(키워드) 생성 -----------------------------------------

    async def generate_search_tags(self, brand: str, raw_title: str, category: str) -> list[str]:
        """쿠팡 검색태그용 키워드 목록을 만든다 (고객이 실제로 검색할 법한 단어 위주)."""
        if self._use_mock:
            return _mock_generate_search_tags(brand, raw_title, category)
        return await self._real_generate_search_tags(brand, raw_title, category)

    async def _real_generate_search_tags(self, brand: str, raw_title: str, category: str) -> list[str]:
        from google.genai import types

        prompt = (
            "너는 쿠팡 검색 키워드 전문가다. 아래 상품을 고객이 쿠팡에서 찾을 때 실제로 "
            "검색창에 칠 법한 키워드를 JSON 문자열 배열 하나로만 답해라(설명 없이 JSON만).\n\n"
            f"브랜드: {brand}\n상품명: {raw_title}\n카테고리: {category}\n\n"
            "규칙:\n"
            f"- 최대 {MAX_SEARCH_TAGS}개, 각 키워드는 공백 포함 {MAX_SEARCH_TAG_LENGTH}자 이내\n"
            "- 용도/대상/특징/스타일 키워드 위주 (예: 남성러닝화, 가벼운운동화, 데일리스니커즈)\n"
            "- 이 상품의 브랜드가 아닌 다른 브랜드명은 절대 넣지 마라 (쿠팡 키워드 어뷰징 제재 대상)\n"
            "- 상품과 무관한 인기 검색어, 최저가/무료배송/정품 같은 과장·홍보 문구, 특수문자 금지"
        )
        try:
            response = await self._sdk_client.aio.models.generate_content(
                model=self._settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=1024,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise GeminiClientError(f"Gemini 검색태그 생성 실패: {exc}") from exc

        try:
            parsed = json.loads(response.text or "")
        except (TypeError, json.JSONDecodeError) as exc:
            raise GeminiClientError(f"Gemini 검색태그 응답이 유효한 JSON이 아닙니다: {response.text!r}") from exc
        if not isinstance(parsed, list):
            raise GeminiClientError(f"Gemini 검색태그 응답이 배열이 아닙니다: {parsed!r}")
        return clean_search_tags([str(tag) for tag in parsed])

    # --- 1-3) 고객문의 답변 초안 -------------------------------------------

    async def draft_inquiry_answer(self, question: str, product_context: str) -> str:
        """쿠팡 고객문의 답변 초안을 만든다 — 대표님이 확인·수정한 뒤에만 실제로 등록된다."""
        if self._use_mock:
            return _mock_draft_inquiry_answer(question)
        return await self._real_draft_inquiry_answer(question, product_context)

    async def _real_draft_inquiry_answer(self, question: str, product_context: str) -> str:
        from google.genai import types

        prompt = (
            "너는 쿠팡에서 브랜드 신발을 파는 판매자의 고객문의 담당자다. 아래 문의에 대한 답변을 "
            "한국어 존댓말로 3~5문장 작성해라. 답변 본문만 출력해라.\n\n"
            f"[상품 정보]\n{product_context}\n\n[고객 문의]\n{question}\n\n"
            "규칙:\n"
            "- 상품 정보에 없는 사실(소재, 발볼, 사이즈 체감, 입고일 등)을 지어내지 마라. 모르면 "
            "'평소 신으시는 사이즈 기준으로 선택하시고 상세페이지 사이즈표를 참고해 주세요'처럼 "
            "일반적인 안내만 하라\n"
            "- 재고는 [상품 정보]의 '구매 가능 사이즈'만 기준으로 답하고, 목록에 없는 사이즈는 "
            "현재 품절이라고 안내해라\n"
            "- 배송: 결제 완료 후 영업일 기준 2~3일 내 출고된다고 안내해라. 정확한 도착일은 약속하지 마라\n"
            # 무료배송 상품은 반품 시 초도배송비+반품배송비(왕복)가 붙어 금액이 경우마다 달라
            # 금액을 직접 말하지 않게 한다.
            "- 단순변심 반품은 반품배송비가 발생하며 금액은 상세페이지 반품/교환 안내를 참고해 "
            "달라고 안내해라(금액을 직접 말하지 마라)\n"
            "- 할인, 사은품, 가격 조정, 교환 확정 같은 약속을 하지 마라\n"
            "- 구매처(어디서 사서 보내는지)는 언급하지 마라\n"
            "- 전화번호/주소 등 개인정보를 묻지 마라\n"
            "- 인사말로 시작하고 감사 인사로 끝내라"
        )
        try:
            response = await self._sdk_client.aio.models.generate_content(
                model=self._settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=1024,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise GeminiClientError(f"Gemini 문의 답변 초안 생성 실패: {exc}") from exc

        answer = (response.text or "").strip()
        if not answer:
            raise GeminiClientError(f"Gemini가 빈 답변을 반환했습니다: {response}")
        return answer

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
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=1024,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise GeminiClientError(f"Gemini 스펙 요약 실패: {exc}") from exc

        if not response.text:
            raise GeminiClientError(f"Gemini가 빈 텍스트를 반환했습니다: {response}")
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


def _mock_generate_search_tags(brand: str, raw_title: str, category: str) -> list[str]:
    words = [w for w in (raw_title or "").split() if w != brand]
    tags = [category, f"{brand}{category}" if brand and category else "", *words]
    return clean_search_tags(tags)


def _mock_draft_inquiry_answer(question: str) -> str:
    return (
        "안녕하세요, 고객님. 문의 주셔서 감사합니다. "
        "결제 완료 후 영업일 기준 2~3일 내 출고되며, 사이즈는 평소 신으시는 사이즈 기준으로 "
        "선택하시고 상세페이지 사이즈표를 참고해 주세요. 감사합니다."
    )


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
