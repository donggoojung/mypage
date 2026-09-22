"""S3StorageClient(실 AWS S3 / Cloudflare R2 등 S3 호환 스토리지) 연동 검증.

boto3는 실제 네트워크를 타므로, boto3.client() 생성 자체를 가짜(Mock)로 바꿔치기해서
"우리 코드가 boto3를 올바른 인자로 호출하는지"와 "URL을 올바르게 조립하는지"를 검증한다
(실제 AWS/R2 계정이 있어야만 도는 테스트가 아니다 — 항상 실행된다).
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings
from app.integrations.storage.s3_client import S3StorageClient, get_storage_client


def _settings(**overrides) -> Settings:
    base = dict(
        aws_access_key_id="AKIA_TEST",
        aws_secret_access_key="secret_test",
        aws_s3_bucket="my-test-bucket",
        aws_region="ap-northeast-2",
        aws_cloudfront_domain="",
        aws_s3_endpoint_url="",
        database_url_sync="sqlite:///:memory:",
    )
    base.update(overrides)
    return Settings(**base)


# --- 1. 필수값 검증 -----------------------------------------------------------


def test_s3_storage_client_requires_bucket():
    with pytest.raises(ValueError, match="AWS_S3_BUCKET"):
        S3StorageClient(_settings(aws_s3_bucket=""))


def test_s3_storage_client_requires_credentials():
    with pytest.raises(ValueError, match="AWS_ACCESS_KEY_ID"):
        S3StorageClient(_settings(aws_access_key_id=""))


# --- 2. boto3 호출/URL 조립 검증 -----------------------------------------------


@patch("app.integrations.storage.s3_client.boto3.client")
def test_upload_bytes_calls_put_object_and_returns_default_aws_url(mock_boto_client):
    mock_s3 = MagicMock()
    mock_boto_client.return_value = mock_s3

    client = S3StorageClient(_settings())
    url = client.upload_bytes(b"fake-image-bytes", key="products/HQ2312/thumbnail.webp", content_type="image/webp")

    mock_s3.put_object.assert_called_once_with(
        Bucket="my-test-bucket",
        Key="products/HQ2312/thumbnail.webp",
        Body=b"fake-image-bytes",
        ContentType="image/webp",
    )
    assert url == "https://my-test-bucket.s3.amazonaws.com/products/HQ2312/thumbnail.webp"


@patch("app.integrations.storage.s3_client.boto3.client")
def test_upload_bytes_uses_cloudfront_domain_when_set(mock_boto_client):
    mock_boto_client.return_value = MagicMock()

    client = S3StorageClient(_settings(aws_cloudfront_domain="pub-abc123.r2.dev"))
    url = client.upload_bytes(b"data", key="products/HQ2312/detail.webp")

    assert url == "https://pub-abc123.r2.dev/products/HQ2312/detail.webp"


@patch("app.integrations.storage.s3_client.boto3.client")
def test_client_passes_endpoint_url_to_boto3_for_r2_compat_storage(mock_boto_client):
    mock_boto_client.return_value = MagicMock()

    S3StorageClient(_settings(aws_s3_endpoint_url="https://myaccount.r2.cloudflarestorage.com"))

    _, kwargs = mock_boto_client.call_args
    assert kwargs["endpoint_url"] == "https://myaccount.r2.cloudflarestorage.com"


@patch("app.integrations.storage.s3_client.boto3.client")
def test_client_passes_none_endpoint_url_for_plain_aws_s3(mock_boto_client):
    mock_boto_client.return_value = MagicMock()

    S3StorageClient(_settings())

    _, kwargs = mock_boto_client.call_args
    assert kwargs["endpoint_url"] is None


@patch("app.integrations.storage.s3_client.boto3.client")
def test_upload_file_reads_local_file_and_uploads(mock_boto_client, tmp_path):
    mock_s3 = MagicMock()
    mock_boto_client.return_value = mock_s3

    local_file = tmp_path / "thumbnail.webp"
    local_file.write_bytes(b"local-file-bytes")

    client = S3StorageClient(_settings())
    client.upload_file(str(local_file), key="products/HQ2312/thumbnail.webp")

    mock_s3.upload_file.assert_called_once_with(
        str(local_file),
        "my-test-bucket",
        "products/HQ2312/thumbnail.webp",
        ExtraArgs={"ContentType": "image/webp"},
    )


@patch("app.integrations.storage.s3_client.boto3.client")
def test_delete_file_calls_delete_object(mock_boto_client):
    mock_s3 = MagicMock()
    mock_boto_client.return_value = mock_s3

    client = S3StorageClient(_settings())
    client.delete_file("products/HQ2312/thumbnail.webp")

    mock_s3.delete_object.assert_called_once_with(Bucket="my-test-bucket", Key="products/HQ2312/thumbnail.webp")


# --- 3. 팩토리(get_storage_client) — Mock/실 전환 -----------------------------


def test_get_storage_client_returns_mock_when_use_mock_storage_true():
    get_storage_client.cache_clear()
    with patch("app.integrations.storage.s3_client.get_settings", return_value=_settings()):
        # aws_s3_bucket이 있어도 use_mock_storage가 True(기본값)면 Mock을 써야 한다.
        client = get_storage_client()
    from app.integrations.storage.s3_client import MockS3StorageClient

    assert isinstance(client, MockS3StorageClient)
    get_storage_client.cache_clear()


@patch("app.integrations.storage.s3_client.boto3.client")
def test_get_storage_client_returns_real_s3_when_use_mock_storage_false(mock_boto_client):
    mock_boto_client.return_value = MagicMock()
    get_storage_client.cache_clear()

    settings = _settings(use_mock_storage=False)
    with patch("app.integrations.storage.s3_client.get_settings", return_value=settings):
        client = get_storage_client()

    assert isinstance(client, S3StorageClient)
    get_storage_client.cache_clear()
