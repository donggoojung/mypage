from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://fulfillment:fulfillment@localhost:5432/fulfillment"
    database_url_sync: str = "postgresql+psycopg2://fulfillment:fulfillment@localhost:5432/fulfillment"

    # Celery / Redis
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # 외부 연동 Mock 스위치 — 실제 키가 준비되기 전까지 true로 두고 로컬 개발한다.
    use_mock_scrapers: bool = True
    use_mock_markets: bool = True
    use_mock_ai_vision: bool = True
    use_mock_messaging: bool = True
    use_mock_rpa: bool = True
    use_mock_storage: bool = True
    # 경쟁가 조회는 플랫폼별로 검증 상태가 달라(네이버=공식 API, 쿠팡=미검증 스크래핑)
    # 하나로 묶지 않고 따로 켜고 끌 수 있게 분리한다.
    use_mock_naver_price_checker: bool = True
    use_mock_coupang_price_checker: bool = True
    # Gemini(LLM) 가공 — 키가 없거나 이 값이 true면 규칙 기반 Mock으로 동작한다.
    use_mock_llm: bool = True

    # 오픈마켓 API 키
    naver_client_id: str = ""
    naver_client_secret: str = ""
    # 네이버 쇼핑검색 오픈API 전용 키 (Commerce API 키와 별개 — https://developers.naver.com/apps 에서 발급)
    naver_search_client_id: str = ""
    naver_search_client_secret: str = ""
    coupang_access_key: str = ""
    coupang_secret_key: str = ""
    coupang_vendor_id: str = ""
    # WING 로그인 아이디(이메일/로그인ID) — 상품 등록 API의 필수 필드(vendorUserId).
    coupang_vendor_user_id: str = ""
    # 반품지 조회 API(returnShippingCenters)가 WING 화면엔 있는 반품지를 빈 배열로
    # 돌려주는 문제(캐시 지연으로 추정)가 있을 때, 이 값을 채워두면 API 조회를 건너뛰고
    # 바로 이 반품지를 쓴다. WING > 판매자정보 > 주소록/배송정보관리에서 반품지 "수정"
    # 눌러 반품지코드/주소를 확인해 채운다. 전부 비워두면(기본값) 예전처럼 API로 자동 조회한다.
    coupang_return_center_code: str = ""
    coupang_return_charge_name: str = ""
    coupang_return_zip_code: str = ""
    coupang_return_address: str = ""
    coupang_return_address_detail: str = ""
    coupang_return_contact_number: str = ""
    esm_master_id: str = ""
    esm_api_key: str = ""

    # 지식재산권 침해 신고/내용증명을 남발해 계정정지 위험이 큰 브랜드 — 쉼표로 구분해
    # 넣으면 크롤링 직후(등록 전) 자동으로 건너뛴다. "섹션 일괄 등록"으로 카테고리 전체를
    # 긁을 때 특히 중요하다(PRD 9.1). 기본값은 비어있음 — 직접 채워야 필터가 동작한다.
    blacklisted_brands: str = ""

    # --- RPA(무인발주) 관련 ---
    # scripts/save_abc_mart_session.py로 저장해둔 ABC마트 로그인 쿠키 파일 경로.
    abc_mart_session_file: str = "scripts/output/abc_mart_session.json"
    # 무신사 로그인 쿠키 파일 경로 — 저장 스크립트는 아직 없음(실사이트 검증 시 작성 필요).
    musinsa_session_file: str = "scripts/output/musinsa_session.json"
    # False(기본값, 안전) = 체크아웃 최종 확인 단계까지만 진행하고 실제 결제 버튼은 누르지 않는다.
    # 실제 자동구매를 완료하려면 이 값을 명시적으로 true로 바꿔야 한다 (쿠팡 request_approval과 같은 안전장치).
    rpa_confirm_final_payment: bool = False

    # AWS S3 / CloudFront (또는 S3 호환 스토리지 — Cloudflare R2 등)
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "ap-northeast-2"
    aws_s3_bucket: str = ""
    aws_cloudfront_domain: str = ""
    # 비워두면 진짜 AWS S3(기본값). Cloudflare R2 등 S3 호환 스토리지를 쓰려면
    # 그 서비스의 엔드포인트(R2는 https://<계정ID>.r2.cloudflarestorage.com)를 넣는다.
    aws_s3_endpoint_url: str = ""

    # 솔라피 (카카오 알림톡 + SMS 자동 폴백, PRD 6.2)
    solapi_api_key: str = ""
    solapi_api_secret: str = ""
    solapi_kakao_pfid: str = ""
    # 사전에 솔라피에 등록해둔 발신번호(문자 대체발송 시 필요) — 등록 안 하면 발신 자체가 거부된다.
    solapi_sender_phone: str = ""
    # 배송출고 안내 알림톡 템플릿 ID — 카카오 채널/템플릿 심사 승인 후 발급된 값을 채운다.
    # 비워두면(기본값) 알림톡 대신 문자(SMS)로만 발송한다.
    solapi_shipping_template_id: str = ""

    # 텔레그램 관리자 긴급 알림 (재고 품절/발주 실패 등 HOLD 상태 발생 시)
    use_mock_telegram: bool = True
    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""

    # AI 연산 API
    replicate_api_token: str = ""
    fal_api_key: str = ""

    # 구글 AI 스튜디오(Gemini) — 상품명 SEO 정제 / 스펙 요약용.
    # https://aistudio.google.com/apikey 에서 발급.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"


@lru_cache
def get_settings() -> Settings:
    return Settings()
