import logging
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from app.core.config import Settings, get_settings
from app.integrations.storage.base import StorageClient

# app/static/ 아래 — FastAPI가 StaticFiles로 "/"에서 서빙하는 디렉터리라,
# 여기 저장된 파일은 브라우저에서 /generated/... 로 바로 열린다.
_STATIC_GENERATED_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "generated"

logger = logging.getLogger(__name__)


class S3StorageClient(StorageClient):
    """실제 AWS S3(또는 S3 호환 스토리지) 업로드 + CDN URL을 조립하는 클라이언트 (PRD 3.3, 8.1).

    AWS_S3_ENDPOINT_URL을 채우면 Cloudflare R2 등 S3 호환 스토리지에도 그대로 쓸 수 있다
    (boto3는 S3 프로토콜만 맞으면 실제 엔드포인트가 어디든 상관없다).
    """

    def __init__(self, settings: Settings):
        if not settings.aws_s3_bucket:
            raise ValueError("AWS_S3_BUCKET 환경변수가 설정되어 있지 않습니다.")
        if not settings.aws_access_key_id or not settings.aws_secret_access_key:
            raise ValueError("AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY 환경변수가 설정되어 있지 않습니다.")
        self._bucket = settings.aws_s3_bucket
        self._cloudfront_domain = settings.aws_cloudfront_domain
        self._endpoint_url = settings.aws_s3_endpoint_url or None
        self._client = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            endpoint_url=self._endpoint_url,
        )

    def _to_public_url(self, key: str) -> str:
        if self._cloudfront_domain:
            return f"https://{self._cloudfront_domain}/{key}"
        if self._endpoint_url:
            # R2 등 S3 호환 스토리지는 "버킷.s3.amazonaws.com" 패턴이 없다 — 공개 접근을
            # 켜둔 버킷이면 엔드포인트 뒤에 버킷/키를 붙인 경로형 URL로도 접근 가능한
            # 경우가 많지만, 서비스마다 달라 AWS_CLOUDFRONT_DOMAIN(퍼블릭 도메인)을
            # 채우는 걸 권장한다는 걸 로그로 남긴다.
            logger.warning(
                "AWS_S3_ENDPOINT_URL은 설정됐지만 AWS_CLOUDFRONT_DOMAIN(퍼블릭 URL)이 없어 "
                "엔드포인트 기반 경로형 URL로 추정합니다 — R2라면 퍼블릭 버킷 도메인을 "
                "AWS_CLOUDFRONT_DOMAIN에 넣는 걸 권장합니다."
            )
            return f"{self._endpoint_url.rstrip('/')}/{self._bucket}/{key}"
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
    """실제 AWS 키 없이 로컬 개발/테스트가 가능한 Mock 구현.

    업로드된 파일은 메모리 딕셔너리에 항상 기록되고, `save_dir`가 주어지면 디스크에도
    실제로 저장한다. `save_dir`가 없으면(단위테스트 기본값) 실 서비스와 동일한 형태의
    가짜 CDN URL만 돌려주고 디스크에는 아무것도 쓰지 않는다 — `save_dir`가 있으면
    브라우저에서 바로 열리는 로컬 경로(`/generated/...`)를 대신 돌려준다.
    """

    def __init__(self, cloudfront_domain: str = "mock-cdn.example.com", save_dir: Path | None = None):
        self._cloudfront_domain = cloudfront_domain
        self._save_dir = save_dir
        self.uploaded: dict[str, bytes] = {}

    def _to_public_url(self, key: str) -> str:
        if self._save_dir is not None:
            return f"/generated/{key}"
        return f"https://{self._cloudfront_domain}/{key}"

    def _save_to_disk(self, key: str, data: bytes) -> None:
        if self._save_dir is None:
            return
        path = self._save_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def upload_file(self, local_path: str, key: str, content_type: str = "image/webp") -> str:
        with open(local_path, "rb") as f:
            data = f.read()
        self.uploaded[key] = data
        self._save_to_disk(key, data)
        return self._to_public_url(key)

    def upload_bytes(self, data: bytes, key: str, content_type: str = "image/webp") -> str:
        self.uploaded[key] = data
        self._save_to_disk(key, data)
        return self._to_public_url(key)

    def delete_file(self, key: str) -> None:
        self.uploaded.pop(key, None)
        if self._save_dir is not None:
            (self._save_dir / key).unlink(missing_ok=True)


@lru_cache
def get_storage_client() -> StorageClient:
    """설정(`USE_MOCK_STORAGE`)에 따라 Mock 또는 실제 S3 클라이언트를 반환하는 팩토리."""
    settings = get_settings()
    if settings.use_mock_storage:
        return MockS3StorageClient(
            settings.aws_cloudfront_domain or "mock-cdn.example.com", save_dir=_STATIC_GENERATED_DIR
        )
    return S3StorageClient(settings)
