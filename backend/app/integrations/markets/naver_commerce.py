"""네이버 커머스 API (스마트스토어) 클라이언트 — 상품 등록, 주문 조회, 발송 처리, 취소·반품 승인.

- Mock(기본값): 가짜 번호만 돌려준다. 실제 스토어에는 아무것도 올라가지 않는다.
- 실제: 네이버 커머스 API. 인증은 client_id/secret 으로 토큰을 받아 쓴다.

주의 (미검증): 이 파일의 실제 호출 부분은 커머스 API 공개 자료와 오픈소스 예시를 보고 작성했지만,
실제 키로 호출해 본 적이 없다. 특히 주문 상세 응답의 항목 이름, 취소·반품 승인 주소는 공식 문서를 직접 보지 못했다.
처음 실제로 켤 때는 스마트스토어센터에서 결과를 눈으로 확인하고, 오류 메시지를 보고 맞춰 가야 한다.
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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


KST = timezone(timedelta(hours=9))


@dataclass
class MarketOrder:
    """스마트스토어 상품주문 하나 (주문 상세에서 우리가 쓰는 것만 추렸다)."""

    product_order_id: str
    order_id: str | None = None
    product_code: str | None = None  # 판매자 상품코드 (우리가 등록할 때 넣은 "agri-상품번호")
    option_code: str | None = None  # 판매자 옵션 관리코드 (공급사 상품코드)
    option_text: str | None = None  # 고객이 고른 옵션 글자
    product_name: str | None = None
    quantity: int = 1
    paid_amount: int = 0
    buyer_name: str | None = None
    receiver_name: str = ""
    receiver_phone: str = ""
    zipcode: str | None = None
    address: str = ""
    memo: str | None = None
    status: str = "PAYED"  # productOrderStatus
    claim_type: str | None = None  # CANCEL / RETURN / EXCHANGE ...
    claim_status: str | None = None  # CANCEL_REQUEST / RETURN_REQUEST / ... / *_DONE
    raw: dict = field(default_factory=dict)


@dataclass
class DispatchItem:
    product_order_id: str
    courier_code: str
    tracking_no: str


@dataclass
class ActionOutcome:
    """주문 하나에 대한 확인·발송·승인 결과."""

    product_order_id: str
    ok: bool
    message: str = ""


class BaseSmartstoreClient(ABC):
    is_mock: bool = False

    @abstractmethod
    async def upload_image(self, image_url: str) -> str:
        """외부 이미지 주소의 사진을 네이버에 올리고, 상품 등록에 쓸 네이버 이미지 주소를 돌려준다."""

    @abstractmethod
    async def register_product(self, payload: dict) -> RegisteredProduct:
        """상품을 등록하고 번호를 돌려준다."""

    @abstractmethod
    async def fetch_orders(self, since: datetime, until: datetime) -> list[MarketOrder]:
        """기간 안에 바뀐 주문(새 주문, 취소·반품 요청 등)을 상세까지 가져온다."""

    @abstractmethod
    async def confirm_orders(self, product_order_ids: list[str]) -> list[ActionOutcome]:
        """발주 확인 (주문을 받았다고 네이버에 알림)."""

    @abstractmethod
    async def dispatch_orders(self, items: list[DispatchItem]) -> list[ActionOutcome]:
        """발송 처리 (택배사와 송장번호를 네이버에 입력)."""

    @abstractmethod
    async def approve_cancel(self, product_order_id: str) -> ActionOutcome:
        """고객의 취소 요청을 승인."""

    @abstractmethod
    async def approve_return(self, product_order_id: str) -> ActionOutcome:
        """고객의 반품 요청을 승인."""


class MockSmartstoreClient(BaseSmartstoreClient):
    """키 없이 개발·시험하는 가짜 클라이언트. 호출 내용을 기록해 두어 시험에서 확인할 수 있다."""

    is_mock = True

    def __init__(self) -> None:
        self.uploaded: list[str] = []
        self.registered: list[dict] = []
        self.orders: list[MarketOrder] = []
        self.confirmed: list[str] = []
        self.dispatched: list[DispatchItem] = []
        self.approved: list[tuple[str, str]] = []
        self.fail_dispatch_for: set[str] = set()

    def add_order(self, **fields) -> MarketOrder:
        """연습용으로 '고객이 주문한 것처럼' 가짜 주문 하나를 만든다."""
        # 서버를 껐다 켜도 이전에 저장된 연습 주문과 번호가 겹치지 않게 무작위 번호를 쓴다
        token = uuid.uuid4().hex[:8].upper()
        data = {"product_order_id": f"MOCK-PO-{token}", "order_id": f"MOCK-O-{token}"}
        data.update(fields)
        order = MarketOrder(**data)
        self.orders.append(order)
        return order

    async def fetch_orders(self, since: datetime, until: datetime) -> list[MarketOrder]:
        return list(self.orders)

    async def confirm_orders(self, product_order_ids: list[str]) -> list[ActionOutcome]:
        self.confirmed.extend(product_order_ids)
        return [ActionOutcome(i, True) for i in product_order_ids]

    async def dispatch_orders(self, items: list[DispatchItem]) -> list[ActionOutcome]:
        results = []
        for item in items:
            if item.product_order_id in self.fail_dispatch_for:
                results.append(ActionOutcome(item.product_order_id, False, "연습용 실패"))
                continue
            self.dispatched.append(item)
            for order in self.orders:
                if order.product_order_id == item.product_order_id:
                    order.status = "DELIVERING"
            results.append(ActionOutcome(item.product_order_id, True))
        return results

    async def approve_cancel(self, product_order_id: str) -> ActionOutcome:
        self.approved.append((product_order_id, "cancel"))
        for order in self.orders:
            if order.product_order_id == product_order_id:
                order.status, order.claim_status = "CANCELED", "CANCEL_DONE"
        return ActionOutcome(product_order_id, True)

    async def approve_return(self, product_order_id: str) -> ActionOutcome:
        self.approved.append((product_order_id, "return"))
        for order in self.orders:
            if order.product_order_id == product_order_id:
                order.status, order.claim_status = "RETURNED", "RETURN_DONE"
        return ActionOutcome(product_order_id, True)

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

    # ------------------------------------------------------------ 주문 (모두 미검증)

    ORDER_API = "/external/v1/pay-order/seller/product-orders"
    MAX_WINDOW = timedelta(hours=24)  # 변경 주문 조회는 한 번에 24시간까지만 (미검증)
    MAX_PAGES_PER_WINDOW = 20

    async def _call(self, method: str, path: str, what: str, *, params=None, json=None) -> dict:
        async with self._http() as http:
            response = await http.request(
                method,
                f"{self._base_url}{path}",
                headers=await self._auth_headers(),
                params=params,
                json=json,
            )
        body = _json_or_text(response)
        if response.status_code not in (200, 201):
            raise NaverCommerceError(f"{what}에 실패했습니다: {_error_text(response, body)}")
        return body if isinstance(body, dict) else {}

    async def fetch_orders(self, since: datetime, until: datetime) -> list[MarketOrder]:
        ids: list[str] = []
        start = since
        while start < until:
            end = min(start + self.MAX_WINDOW, until)
            ids.extend(await self._changed_ids(start, end))
            start = end
        unique = list(dict.fromkeys(ids))
        orders: list[MarketOrder] = []
        for i in range(0, len(unique), 300):
            body = await self._call(
                "POST",
                f"{self.ORDER_API}/query",
                "주문 상세 조회",
                json={"productOrderIds": unique[i : i + 300]},
            )
            for item in body.get("data") or []:
                orders.append(_parse_market_order(item))
        return orders

    async def _changed_ids(self, start: datetime, end: datetime) -> list[str]:
        ids: list[str] = []
        params: dict = {"lastChangedFrom": _kst_iso(start), "lastChangedTo": _kst_iso(end), "limitCount": 300}
        for _ in range(self.MAX_PAGES_PER_WINDOW):
            body = await self._call("GET", f"{self.ORDER_API}/last-changed-statuses", "변경 주문 조회", params=params)
            data = body.get("data") or {}
            ids.extend(str(s["productOrderId"]) for s in data.get("lastChangeStatuses") or [] if "productOrderId" in s)
            more = data.get("more")
            if not more or not more.get("moreFrom"):
                break
            params = {**params, "lastChangedFrom": more["moreFrom"], "moreSequence": more.get("moreSequence")}
            params = {k: v for k, v in params.items() if v is not None}
        return ids

    @staticmethod
    def _outcomes(body: dict, ids: list[str], what: str) -> list[ActionOutcome]:
        """성공·실패 목록이 섞여 오는 응답을 주문별 결과로 바꾼다."""
        data = body.get("data") or {}
        failed = {
            str(f.get("productOrderId")): f.get("message") or f.get("code") or "이유를 알 수 없음"
            for f in data.get("failProductOrderInfos") or []
        }
        return [ActionOutcome(i, i not in failed, f"{what}: {failed[i]}" if i in failed else "") for i in ids]

    async def confirm_orders(self, product_order_ids: list[str]) -> list[ActionOutcome]:
        if not product_order_ids:
            return []
        body = await self._call(
            "POST", f"{self.ORDER_API}/confirm", "발주 확인", json={"productOrderIds": product_order_ids}
        )
        return self._outcomes(body, product_order_ids, "발주 확인")

    async def dispatch_orders(self, items: list[DispatchItem]) -> list[ActionOutcome]:
        if not items:
            return []
        now = _kst_iso(datetime.now(KST))
        payload = {
            "dispatchProductOrders": [
                {
                    "productOrderId": i.product_order_id,
                    "deliveryMethod": "DELIVERY",
                    "deliveryCompanyCode": i.courier_code,
                    "trackingNumber": i.tracking_no,
                    "dispatchDate": now,
                }
                for i in items
            ]
        }
        body = await self._call("POST", f"{self.ORDER_API}/dispatch", "발송 처리", json=payload)
        return self._outcomes(body, [i.product_order_id for i in items], "발송 처리")

    async def approve_cancel(self, product_order_id: str) -> ActionOutcome:
        await self._call("POST", f"{self.ORDER_API}/{product_order_id}/claim/cancel/approve", "취소 승인")
        return ActionOutcome(product_order_id, True)

    async def approve_return(self, product_order_id: str) -> ActionOutcome:
        await self._call("POST", f"{self.ORDER_API}/{product_order_id}/claim/return/approve", "반품 승인")
        return ActionOutcome(product_order_id, True)


def _kst_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=KST)
    return moment.astimezone(KST).isoformat(timespec="milliseconds")


def _parse_market_order(item: dict) -> MarketOrder:
    """주문 상세 한 건(order + productOrder)을 우리가 쓰는 모양으로. 항목 이름은 미검증."""
    po = item.get("productOrder") or {}
    order = item.get("order") or {}
    address = po.get("shippingAddress") or {}
    full_address = " ".join(p for p in (address.get("baseAddress"), address.get("detailedAddress")) if p)
    return MarketOrder(
        product_order_id=str(po.get("productOrderId", "")),
        order_id=str(order["orderId"]) if order.get("orderId") is not None else None,
        product_code=po.get("sellerProductCode"),
        option_code=po.get("optionManageCode") or po.get("optionCode"),
        option_text=po.get("productOption"),
        product_name=po.get("productName"),
        quantity=int(po.get("quantity") or 1),
        paid_amount=int(po.get("totalPaymentAmount") or 0),
        buyer_name=order.get("ordererName"),
        receiver_name=address.get("name") or "",
        receiver_phone=address.get("tel1") or address.get("tel2") or "",
        zipcode=address.get("zipCode"),
        address=full_address,
        memo=po.get("shippingMemo"),
        status=po.get("productOrderStatus") or "",
        claim_type=po.get("claimType"),
        claim_status=po.get("claimStatus"),
        raw=item,
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
