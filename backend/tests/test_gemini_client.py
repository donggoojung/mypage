"""Gemini(LLM) 연동 클라이언트 검증.

소싱처(ABC마트 등) 원본 상품명/상세텍스트를 SEO 상품명·정형 스펙 JSON으로 가공하는
GeminiClient가 Mock 모드(키 없음/use_mock=True)와 실제 API 키 모드 양쪽에서 정상
동작하는지 확인한다.
"""

import os

import pytest

from app.core.config import Settings
from app.integrations.llm.gemini_client import GeminiClient, REQUIRED_SPEC_KEYS

RAW_DETAIL_TEXT = """상품 상세 안내
소재: 천연가죽
색상: 화이트
굽높이: 3cm
이 제품은 통기성이 좋습니다.
"""


def _mock_settings() -> Settings:
    return Settings(gemini_api_key="", use_mock_llm=True, database_url_sync="sqlite:///:memory:")


# --- 1. Mock 모드: 상품명 SEO 정제 -------------------------------------------


@pytest.mark.asyncio
async def test_generate_seo_title_mock_formats_brand_and_style_code():
    client = GeminiClient(settings=_mock_settings())

    title = await client.generate_seo_title(
        brand="나이키", raw_title="나이키 메트로 TEK", style_code="HQ2312", category="운동화"
    )

    assert title.startswith("[나이키]")
    assert "HQ2312" in title
    assert "운동화" in title
    # 브랜드가 원본 상품명에도 포함돼 있던 경우("나이키 나이키 ...") 중복 표기되면 안 된다.
    assert title.count("나이키") == 1


@pytest.mark.asyncio
async def test_generate_seo_title_mock_strips_repeated_brand_prefix():
    """실제 스크래핑 데이터에서 실제로 나온 패턴: 원본 상품명 앞에 브랜드가 2번 겹침."""
    client = GeminiClient(settings=_mock_settings())

    title = await client.generate_seo_title(
        brand="나이키", raw_title="나이키 나이키 메트로 TEK", style_code="HQ2312", category="운동화"
    )

    assert title.count("나이키") == 1


@pytest.mark.asyncio
async def test_generate_seo_title_mock_respects_length_limit():
    client = GeminiClient(settings=_mock_settings())

    title = await client.generate_seo_title(
        brand="누오보",
        raw_title="누오보 정말정말정말정말정말정말정말정말정말 긴 상품명 테스트용 문자열입니다",
        style_code="NC40160",
        category="여성 정장구두",
    )

    assert len(title) <= 50


@pytest.mark.asyncio
async def test_generate_seo_title_mock_handles_missing_brand():
    client = GeminiClient(settings=_mock_settings())

    title = await client.generate_seo_title(brand="", raw_title="무브랜드 운동화", style_code="X1", category="신발")

    assert "[" not in title  # 브랜드 없으면 빈 대괄호를 만들지 않는다.
    assert "무브랜드 운동화" in title


# --- 2. Mock 모드: 상세 스펙 요약 ---------------------------------------------


@pytest.mark.asyncio
async def test_summarize_product_specs_mock_extracts_key_value_lines():
    client = GeminiClient(settings=_mock_settings())

    specs = await client.summarize_product_specs(RAW_DETAIL_TEXT)

    assert specs["소재"] == "천연가죽"
    assert specs["색상"] == "화이트"
    assert specs["굽높이"] == "3cm"


@pytest.mark.asyncio
async def test_summarize_product_specs_mock_fills_missing_required_keys():
    client = GeminiClient(settings=_mock_settings())

    specs = await client.summarize_product_specs("특이사항 없는 평범한 설명 텍스트")

    for key in REQUIRED_SPEC_KEYS:
        assert key in specs
        assert specs[key]  # 빈 문자열이면 안 됨 — 기본값으로라도 채워져야 한다.


@pytest.mark.asyncio
async def test_summarize_product_specs_mock_returns_dict_for_empty_input():
    client = GeminiClient(settings=_mock_settings())

    specs = await client.summarize_product_specs("")

    assert isinstance(specs, dict)
    assert set(REQUIRED_SPEC_KEYS).issubset(specs.keys())


# --- 3. use_mock 자동 판별 ---------------------------------------------------


def test_client_defaults_to_mock_when_no_api_key():
    client = GeminiClient(settings=_mock_settings())
    assert client._use_mock is True  # noqa: SLF001 - 내부 플래그를 직접 검증


def test_client_uses_mock_when_use_mock_llm_true_even_with_key():
    settings = Settings(gemini_api_key="dummy-key", use_mock_llm=True, database_url_sync="sqlite:///:memory:")
    client = GeminiClient(settings=settings)
    assert client._use_mock is True  # noqa: SLF001


def test_client_real_mode_requires_google_genai_or_raises_clear_error():
    settings = Settings(gemini_api_key="dummy-key", use_mock_llm=False, database_url_sync="sqlite:///:memory:")
    try:
        import google.genai  # noqa: F401

        pytest.skip("google-genai가 설치되어 있어 이 실패 경로를 재현할 수 없습니다.")
    except ImportError:
        pass

    from app.integrations.llm.gemini_client import GeminiClientError

    with pytest.raises(GeminiClientError, match="google-genai"):
        GeminiClient(settings=settings)


# --- 4. 실제 API 키가 .env에 있을 때만 실행되는 실계정 검증 -------------------


def _real_gemini_api_key() -> str:
    """.env(get_settings())와 실제 셸 환경변수 양쪽을 다 확인한다 —
    pydantic-settings는 .env 값을 os.environ에 반영하지 않으므로 os.environ만 보면
    .env에만 키를 넣어둔 경우를 놓친다.
    """
    from app.core.config import get_settings

    return os.environ.get("GEMINI_API_KEY") or get_settings().gemini_api_key


def _real_settings_or_none() -> Settings | None:
    api_key = _real_gemini_api_key()
    if not api_key:
        return None
    return Settings(gemini_api_key=api_key, use_mock_llm=False, database_url_sync="sqlite:///:memory:")


@pytest.mark.asyncio
@pytest.mark.skipif(not _real_gemini_api_key(), reason="GEMINI_API_KEY가 .env에 없어 실API 테스트를 건너뜁니다.")
async def test_generate_seo_title_real_api_returns_nonempty_title():
    client = GeminiClient(settings=_real_settings_or_none())

    title = await client.generate_seo_title(
        brand="나이키", raw_title="나이키 메트로 TEK", style_code="HQ2312", category="운동화"
    )

    print(f"\n[실API] 생성된 SEO 상품명: {title!r}")
    assert title
    assert len(title) <= 50


@pytest.mark.asyncio
@pytest.mark.skipif(not _real_gemini_api_key(), reason="GEMINI_API_KEY가 .env에 없어 실API 테스트를 건너뜁니다.")
async def test_summarize_product_specs_real_api_returns_dict():
    client = GeminiClient(settings=_real_settings_or_none())

    specs = await client.summarize_product_specs(RAW_DETAIL_TEXT)

    print(f"\n[실API] 요약된 스펙: {specs!r}")
    assert isinstance(specs, dict)
    assert specs
