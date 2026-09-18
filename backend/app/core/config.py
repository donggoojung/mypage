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

    # 오픈마켓 API 키
    naver_client_id: str = ""
    naver_client_secret: str = ""
    coupang_access_key: str = ""
    coupang_secret_key: str = ""
    coupang_vendor_id: str = ""
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
