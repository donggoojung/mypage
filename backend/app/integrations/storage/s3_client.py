import logging
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

from app.core.config import Settings, get_settings
from app.integrations.storage.base import StorageClient

logger = logging.getLogger(__name__)


class S3StorageClient(StorageClient):
    """실제 AWS S3 업로드 + CloudFront 도메인으로 CDN URL을 조립하는 클라이언트 (PRD 3.3, 8.1)."""

    def __init__(self, settings: Settings):
        if not settings.aws_s3_bucket:
            raise ValueError("AWS_S3_BUCKET 환경변수가 설정되어 있지 않습니다.")
        self._bucket = settings.aws_s3_bucket
        self._cloudfront_domain = settings.aws_cloudfront_domain
        self._client = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id or None,
            aws_secret_access_key=settings.aws_secret_access_key or None,
        )

    def _to_public_url(self, key: str) -> str:
        if self._cloudfront_domain:
            return f"https://{self._cloudfront_domain}/{key}"
        return f"https://{self._bucket}.s3.amazonaws.com/{key}"

    def upload_file(self, local_path: str, key: str, content_type: str = "image/webp") -> str:
        try:
            self._client.upload_file(
                local_path, self._bucket, key, ExtraArgs={"ContentType": content_type}
            )
        except ClientError:
            logger.exception("S3 upload_file 실패: bucket=%s key=%s", self._bucket, key)
            raise
        return self._to_public_url(key)

    def upload_bytes(self, data: bytes, key: str, content_type: str = "image/webp") -> str:
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
        except ClientError:
            logger.exception("S3 put_object 실패: bucket=%s key=%s", self._bucket, key)
            raise
        return self._to_public_url(key)

    def delete_file(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)


class MockS3StorageClient(StorageClient):
    """실제 AWS 키 없이 로컬 개발/테스트가 가능한 인메모리 Mock 구현.

    업로드된 파일을 실제로 전송하지 않고 메모리 딕셔너리에 기록만 하며,
    실 서비스와 동일한 형태의 가짜 CDN URL을 반환한다.
    """

    def __init__(self, cloudfront_domain: str = "mock-cdn.example.com"):
        self._cloudfront_domain = cloudfront_domain
        self.uploaded: dict[str, bytes] = {}

    def _to_public_url(self, key: str) -> str:
        return f"https://{self._cloudfront_domain}/{key}"

    def upload_file(self, local_path: str, key: str, content_type: str = "image/webp") -> str:
        with open(local_path, "rb") as f:
            self.uploaded[key] = f.read()
        return self._to_public_url(key)

    def upload_bytes(self, data: bytes, key: str, content_type: str = "image/webp") -> str:
        self.uploaded[key] = data
        return self._to_public_url(key)

    def delete_file(self, key: str) -> None:
        self.uploaded.pop(key, None)


@lru_cache
def get_storage_client() -> StorageClient:
    """설정(`USE_MOCK_STORAGE`)에 따라 Mock 또는 실제 S3 클라이언트를 반환하는 팩토리."""
    settings = get_settings()
    if settings.use_mock_storage:
        return MockS3StorageClient(settings.aws_cloudfront_domain or "mock-cdn.example.com")
    return S3StorageClient(settings)
