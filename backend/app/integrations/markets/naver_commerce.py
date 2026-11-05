"""네이버 커머스 API (스마트스토어) 클라이언트 — 상품 등록.

- Mock(기본값): 가짜 번호만 돌려준다. 실제 스토어에는 아무것도 올라가지 않는다.
- 실제: 네이버 커머스 API. 인증은 client_id/secret 으로 토큰을 받아 쓴다.

주의 (미검증): 이 파일의 실제 호출 부분은 커머스 API 공개 자료를 보고 작성했지만, 실제 키로 호출해 본 적이 없다.
처음 실제로 켤 때는 스마트스토어센터에서 결과를 눈으로 확인하고, 오류 메시지를 보고 맞춰 가야 한다.
"""

from __future__ import annotations

import base64
import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from app.core.config import Settings, get_settings

NAVER_COMMERCE_BASE_URL = "https://api.commerce.naver.com"


class NaverCommerceError(Exception):
    """네이버가 요청을 거절했거나 연결에 실패했을 때."""


@dataclass
class RegisteredProduct:
    origin_product_no: str
    channel_product_no: str | None = None
    raw: dict = field(default_factory=dict)


class BaseSmartstoreClient(ABC):
    is_mock: bool = False

    @abstractmethod
    async def upload_image(self, image_url: str) -> str:
        """외부 이미지 주소의 사진을 네이버에 올리고, 상품 등록에 쓸 네이버 이미지 주소를 돌려준다."""

    @abstractmethod
    async def register_product(self, payload: dict) -> RegisteredProduct:
        """상품을 등록하고 번호를 돌려준다."""


class MockSmartstoreClient(BaseSmartstoreClient):
    """키 없이 개발·시험하는 가짜 클라이언트. 호출 내용을 기록해 두어 시험에서 확인할 수 있다."""

    is_mock = True

    def __init__(self) -> None:
        self.uploaded: list[str] = []
        self.registered: list[dict] = []

    async def upload_image(self, image_url: str) -> str:
        self.uploaded.append(image_url)
        digest = hashlib.md5(image_url.encode("utf-8")).hexdigest()[:12]
        return f"https://shop-phinf.pstatic.net/mock/{digest}.jpg"

    async def register_product(self, payload: dict) -> RegisteredProduct:
        self.registered.append(payload)
        number = 100000 + len(self.registered)
        return RegisteredProduct(
            origin_product_no=f"MOCK-{number}",
            channel_product_no=f"MOCK-CH-{number}",
            raw={"mock": True},
        )


def make_client_secret_sign(client_id: str, client_secret: str, timestamp_ms: int) -> str:
    """토큰 요청에 붙이는 서명. client_secret(bcrypt 솔트)으로 "client_id_timestamp"를 bcrypt 해시한 뒤 base64."""
    import bcrypt

    hashed = bcrypt.hashpw(f"{client_id}_{timestamp_ms}".encode("utf-8"), client_secret.encode("utf-8"))
    return base64.b64encode(hashed).decode("utf-8")


class NaverCommerceClient(BaseSmartstoreClient):
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = NAVER_COMMERCE_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 설정되어 있지 않습니다.")
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=30.0, transport=self._transport)

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        timestamp = int(time.time() * 1000)
        data = {
            "client_id": self._client_id,
            "timestamp": timestamp,
            "client_secret_sign": make_client_secret_sign(self._client_id, self._client_secret, timestamp),
            "grant_type": "client_credentials",
            "type": "SELF",
        }
        async with self._http() as http:
            response = await http.post(f"{self._base_url}/external/v1/oauth2/token", data=data)
        body = _json_or_text(response)
        if response.status_code != 200 or "access_token" not in body:
            raise NaverCommerceError(f"네이버 로그인(토큰 발급)에 실패했습니다: {_error_text(response, body)}")
        self._token = body["access_token"]
        self._token_expires_at = time.time() + float(body.get("expires_in", 3600))
        return self._token

    async def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {await self._access_token()}"}

    async def upload_image(self, image_url: str) -> str:
        async with self._http() as http:
            try:
                source = await http.get(image_url, follow_redirects=True)
                source.raise_for_status()
            except httpx.HTTPError as exc:
                raise NaverCommerceError(f"상품 사진을 내려받지 못했습니다: {image_url} ({exc})") from exc
            content_type = source.headers.get("content-type", "image/jpeg").split(";")[0]
            files = {"imageFiles": ("product.jpg", source.content, content_type)}
            response = await http.post(
                f"{self._base_url}/external/v1/product-images/upload",
                headers=await self._auth_headers(),
                files=files,
            )
        body = _json_or_text(response)
        images = body.get("images") if isinstance(body, dict) else None
        if response.status_code != 200 or not images:
            raise NaverCommerceError(f"사진 올리기에 실패했습니다: {_error_text(response, body)}")
        return images[0]["url"]

    async def register_product(self, payload: dict) -> RegisteredProduct:
        async with self._http() as http:
            response = await http.post(
                f"{self._base_url}/external/v2/products",
                headers={**await self._auth_headers(), "Content-Type": "application/json"},
                json=payload,
            )
        body = _json_or_text(response)
        if response.status_code not in (200, 201) or not isinstance(body, dict) or "originProductNo" not in body:
            raise NaverCommerceError(f"상품 등록이 거절되었습니다: {_error_text(response, body)}")
        channel_no = body.get("smartstoreChannelProductNo")
        return RegisteredProduct(
            origin_product_no=str(body["originProductNo"]),
            channel_product_no=str(channel_no) if channel_no is not None else None,
            raw=body,
        )


def _json_or_text(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return response.text


def _error_text(response: httpx.Response, body) -> str:
    if isinstance(body, dict):
        message = body.get("message") or body.get("error_description") or body.get("error") or ""
        invalid = body.get("invalidInputs")
        if invalid:
            details = "; ".join(f"{i.get('name', '?')}: {i.get('message', '')}" for i in invalid if isinstance(i, dict))
            message = f"{message} ({details})"
        return f"[{response.status_code}] {message}".strip()
    return f"[{response.status_code}] {str(body)[:300]}"


_real_client: NaverCommerceClient | None = None
_mock_client: MockSmartstoreClient | None = None


def get_smartstore_client(settings: Settings | None = None) -> BaseSmartstoreClient:
    """USE_MOCK_NAVER_COMMERCE 에 따라 가짜 또는 실제 클라이언트를 돌려준다 (토큰을 재사용하려고 하나만 만든다)."""
    global _real_client, _mock_client
    settings = settings or get_settings()
    if settings.use_mock_naver_commerce:
        if _mock_client is None:
            _mock_client = MockSmartstoreClient()
        return _mock_client
    if _real_client is None:
        _real_client = NaverCommerceClient(settings.naver_client_id, settings.naver_client_secret)
    return _real_client
