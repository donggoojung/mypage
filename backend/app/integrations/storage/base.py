from abc import ABC, abstractmethod


class StorageClient(ABC):
    """AI 생성 썸네일/상세페이지 WebP 파일을 업로드하는 스토리지 클라이언트 인터페이스.

    PRD 3.3: 원본 이미지의 소싱처 흔적(EXIF, URL)을 세탁한 뒤 자체 S3 버킷에 저장하고,
    CloudFront CDN 주소로 반환하여 마켓 API에 전송한다.
    """

    @abstractmethod
    def upload_file(self, local_path: str, key: str, content_type: str = "image/webp") -> str:
        """로컬 파일을 업로드하고, 마켓 API로 전송할 CDN(퍼블릭) URL을 반환한다."""
        raise NotImplementedError

    @abstractmethod
    def upload_bytes(self, data: bytes, key: str, content_type: str = "image/webp") -> str:
        """메모리 상의 바이트(예: 렌더링 직후의 이미지)를 업로드하고 CDN URL을 반환한다."""
        raise NotImplementedError

    @abstractmethod
    def delete_file(self, key: str) -> None:
        """업로드된 파일을 삭제한다 (오등록 롤백, 재생성 시 이전 에셋 정리 등에 사용)."""
        raise NotImplementedError
