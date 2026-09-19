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

    # 오픈마켓 API 키
    naver_client_id: str = ""
    naver_client_secret: str = ""
    # 네이버 쇼핑검색 오픈API 전용 키 (Commerce API 키와 별개 — https://developers.naver.com/apps 에서 발급)
    naver_search_client_id: str = ""
    naver_search_client_secret: str = ""
    coupang_access_key: str = ""
    coupang_secret_key: str = ""
    coupang_vendor_id: str = ""
    # 쿠팡 WING 판매자 계정에 등록된 값 — 코드로 추측할 수 없는 계정 고유 설정값이라
    # 실제 등록 전 사용자가 WING 판매자센터에서 직접 확인해 채워야 한다.
    coupang_return_center_code: str = ""
    coupang_outbound_shipping_place_code: str = ""
    coupang_return_charge_name: str = ""
    coupang_company_contact_number: str = ""
    coupang_return_zip_code: str = ""
    coupang_return_address: str = ""
    coupang_return_address_detail: str = ""
    esm_master_id: str = ""
    esm_api_key: str = ""

    # AWS S3 / CloudFront
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "ap-northeast-2"
    aws_s3_bucket: str = ""
    aws_cloudfront_domain: str = ""

    # 솔라피
    solapi_api_key: str = ""
    solapi_api_secret: str = ""
    solapi_kakao_pfid: str = ""

    # AI 연산 API
    replicate_api_token: str = ""
    fal_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
