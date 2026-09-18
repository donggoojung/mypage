import pytest

from app.integrations.storage.s3_client import MockS3StorageClient


@pytest.fixture
def storage():
    return MockS3StorageClient(cloudfront_domain="cdn.test.example.com")


def test_upload_bytes_returns_cdn_url_and_stores_content(storage):
    url = storage.upload_bytes(b"fake-webp-bytes", key="products/CW2288-111/thumbnail.webp")

    assert url == "https://cdn.test.example.com/products/CW2288-111/thumbnail.webp"
    assert storage.uploaded["products/CW2288-111/thumbnail.webp"] == b"fake-webp-bytes"


def test_upload_file_reads_local_file(storage, tmp_path):
    local_file = tmp_path / "detail.webp"
    local_file.write_bytes(b"detail-page-bytes")

    url = storage.upload_file(str(local_file), key="products/CW2288-111/detail.webp")

    assert url.endswith("products/CW2288-111/detail.webp")
    assert storage.uploaded["products/CW2288-111/detail.webp"] == b"detail-page-bytes"


def test_delete_file_removes_stored_content(storage):
    storage.upload_bytes(b"to-be-deleted", key="tmp/asset.webp")
    storage.delete_file("tmp/asset.webp")

    assert "tmp/asset.webp" not in storage.uploaded


def test_delete_file_is_noop_for_missing_key(storage):
    storage.delete_file("does/not/exist.webp")  # 예외 없이 통과해야 한다.
