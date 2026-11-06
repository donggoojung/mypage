"""토스쇼핑 API 클라이언트 — 상품 등록, 주문 가져오기, 송장 입력, 발송 지연, 취소·반품 처리.

- Mock(기본값): 가짜 응답만 돌려준다. 실제 토스쇼핑에는 아무것도 가지 않는다.
- 실제: https://shopping-fep.toss.im (인증키 + 등록된 서버 IP 필요).

근거: 토스쇼핑 파트너스 개발 문서(https://shopping-docs.toss.im)의 OpenAPI 명세를 그대로 옮겼다 (`agri/토스쇼핑_연동_메모.md`).
아직 실제 키로 호출해 본 적은 없다.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from app.core.config import Settings, get_settings

PROD_API = "https://shopping-fep.toss.im"
ALPHA_API = "https://shopping-fep-alpha.toss.im"
PROD_TOKEN = "https://oauth2.cert.toss.im/token"
ALPHA_TOKEN = "https://oauth2-alpha.cert.toss.im/token"
API = "/api/v3/shopping-fep"


class TossError(Exception):
    """토스쇼핑이 요청을 거절했거나 연결에 실패했을 때 (화면에 그대로 보여준다)."""


@dataclass
class TossOrder:
    """주문 상품 한 줄 (주문 내역 조회 응답에서 우리가 쓰는 것만)."""

    order_product_id: str
    order_id: str | None = None
    product_code: str | None = None  # 우리가 등록할 때 넣은 상품 관리 코드 (agri-상품번호)
    item_code: str | None = None  # 옵션 관리 코드 (agri-옵션번호)
    product_name: str | None = None
    option_name: str | None = None
    quantity: int = 1
    price: int = 0  # 판매가 주문 총금액 (판매가 × 수량)
    buyer_name: str | None = None
    receiver_name: str = ""
    receiver_phone: str = ""
    zipcode: str | None = None
    address: str = ""
    memo: str | None = None
    status: str = "PAID"  # orderProductStatus
    shipping_deadline: str | None = None
    delivery_company: str | None = None
    tracking_no: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class TossClaim:
    claim_id: str
    type: str  # CANCEL / RETURN / EXCHANGE
    status: str  # REQUESTED ...
    order_product_id: str
    reason: str = ""


@dataclass
class Outcome:
    key: str
    ok: bool
    message: str = ""


class BaseTossClient(ABC):
    is_mock: bool = False

    # 도움말 조회 (설정 값 찾기용, 읽기만 한다)
    @abstractmethod
    async def categories(self, parent_id: int | None = None) -> list[dict]: ...

    @abstractmethod
    async def category_template(self, category_id: int) -> dict: ...

    @abstractmethod
    async def notice_category_codes(self) -> list[dict]: ...

    @abstractmethod
    async def notice_items(self, category_code: str) -> list[dict]: ...

    @abstractmethod
    async def delivery_locations(self) -> list[dict]: ...

    @abstractmethod
    async def exchange_refund_locations(self) -> list[dict]: ...

    @abstractmethod
    async def delivery_companies(self) -> list[str]: ...

    # 상품
    @abstractmethod
    async def register_product(self, payload: dict) -> int:
        """상품을 등록하고 토스쇼핑 상품 ID를 돌려준다 (등록 뒤 검수를 거쳐 노출된다)."""

    # 주문
    @abstractmethod
    async def fetch_orders(self, start: date, end: date) -> list[TossOrder]: ...

    @abstractmethod
    async def dispatch(self, order_product_id: str, company: str, tracking_no: str) -> Outcome:
        """송장 입력 (주문이 배송중이 된다)."""

    @abstractmethod
    async def delay_shipping(self, order_product_ids: list[str], deadline: datetime, reason: str) -> list[Outcome]:
        """발송 지연 (주문 상품당 1번만, 오늘 기준 1~32일 뒤)."""

    # 클레임
    @abstractmethod
    async def fetch_claims(self, claim_type: str, status: str = "REQUESTED") -> list[TossClaim]: ...

    @abstractmethod
    async def approve_cancel(self, claim_id: str) -> Outcome: ...

    @abstractmethod
    async def reject_cancel(self, claim_id: str, company: str, tracking_no: str) -> Outcome:
        """취소 거절 = 이미 출고했다는 뜻. 택배사·송장번호를 넣으면 주문이 배송중이 된다."""

    @abstractmethod
    async def approve_return(self, claim_id: str) -> Outcome: ...


class MockTossClient(BaseTossClient):
    """키 없이 개발·시험하는 가짜 클라이언트. 호출 내용을 기록해 둔다."""

    is_mock = True

    def __init__(self) -> None:
        self.registered: list[dict] = []
        self.orders: list[TossOrder] = []
        self.claims: list[TossClaim] = []
        self.dispatched: list[tuple[str, str, str]] = []
        self.delays: list[tuple[list[str], datetime, str]] = []
        self.approved: list[str] = []
        self.rejected: list[tuple[str, str, str]] = []
        self.returns_approved: list[str] = []
        self.fail_dispatch_for: set[str] = set()
        self._counter = 0

    # 도움말 조회: 설정 화면에서 쓰는 모양을 연습해 볼 수 있게 가짜 값을 돌려준다
    async def categories(self, parent_id: int | None = None) -> list[dict]:
        tree = {
            None: [{"id": 100, "name": "식품", "level": 1, "parentId": None, "isLeaf": False}],
            100: [
                {"id": 101, "name": "과일", "level": 2, "parentId": 100, "isLeaf": True},
                {"id": 102, "name": "채소", "level": 2, "parentId": 100, "isLeaf": True},
                {"id": 103, "name": "쌀/잡곡", "level": 2, "parentId": 100, "isLeaf": True},
            ],
        }
        return tree.get(parent_id, [])

    async def category_template(self, category_id: int) -> dict:
        return {
            "categoryId": category_id,
            "categoryName": "식품 > 연습용",
            "categorySalesOptions": [
                {"id": 1, "key": "중량", "valueCandidates": [], "isOption": False, "unitValues": ["g", "kg"], "isOneOfRequiredGroup": False}
            ],
            "productNoticeInfoTemplateTypes": ["AGRI_FOOD"],
        }

    async def notice_category_codes(self) -> list[dict]:
        return [{"categoryCode": "AGRI_FOOD", "firstCategoryName": "식품", "secondCategoryName": "농수축산물 (연습용)"}]

    async def notice_items(self, category_code: str) -> list[dict]:
        titles = [
            "1. 품목 또는 명칭", "2. 포장단위별 내용물의 용량(중량), 수량, 크기", "3. 생산자, 수입자",
            "4. 원산지", "5. 제조연월일, 소비기한 또는 품질유지기한", "6. 관련법상 표시사항(유전자변형, 지리적 표시)",
            "7. 상품구성", "8. 보관방법 또는 취급방법", "9. 소비자 안전을 위한 주의사항", "10. 소비자 상담 관련 전화번호",
        ]
        return [{"id": 900 + i, "title": t} for i, t in enumerate(titles, start=1)]

    async def delivery_locations(self) -> list[dict]:
        return [{"id": 1001, "name": "기본 배송 그룹 (연습용)", "isMain": True}]

    async def exchange_refund_locations(self) -> list[dict]:
        return [{"id": 2001, "zipCode": "06234", "address": "서울특별시 강남구 (연습용)", "detailAddress": "1층", "isMain": True}]

    async def delivery_companies(self) -> list[str]:
        return ["CJ대한통운", "우체국택배", "한진택배", "롯데택배", "로젠택배", "직접전달"]

    async def register_product(self, payload: dict) -> int:
        self.registered.append(payload)
        self._counter += 1
        return 880000 + self._counter

    def add_order(self, **fields) -> TossOrder:
        """연습용으로 '고객이 주문한 것처럼' 가짜 주문을 만든다."""
        token = uuid.uuid4().int % 10**9
        data = {"order_product_id": f"MOCK-{token}", "order_id": f"MOCK-O-{token}"}
        data.update(fields)
        order = TossOrder(**data)
        self.orders.append(order)
        return order

    async def fetch_orders(self, start: date, end: date) -> list[TossOrder]:
        return list(self.orders)

    def _order(self, order_product_id: str) -> TossOrder | None:
        return next((o for o in self.orders if o.order_product_id == order_product_id), None)

    async def dispatch(self, order_product_id: str, company: str, tracking_no: str) -> Outcome:
        if order_product_id in self.fail_dispatch_for:
            return Outcome(order_product_id, False, "연습용 실패")
        self.dispatched.append((order_product_id, company, tracking_no))
        order = self._order(order_product_id)
        if order:
            order.status, order.delivery_company, order.tracking_no = "DELIVERING", company, tracking_no
        return Outcome(order_product_id, True)

    async def delay_shipping(self, order_product_ids: list[str], deadline: datetime, reason: str) -> list[Outcome]:
        self.delays.append((order_product_ids, deadline, reason))
        return [Outcome(i, True) for i in order_product_ids]

    def add_cancel_request(self, order: TossOrder) -> TossClaim:
        """연습용으로 고객이 (상품준비중 이후에) 취소를 요청한 것처럼 만든다."""
        order.status = "CLAIM_REQUESTED_CANCEL"
        claim = TossClaim(f"MOCKCLM{uuid.uuid4().hex[:8].upper()}", "CANCEL", "REQUESTED", order.order_product_id, "단순 변심")
        self.claims.append(claim)
        return claim

    async def fetch_claims(self, claim_type: str, status: str = "REQUESTED") -> list[TossClaim]:
        return [c for c in self.claims if c.type == claim_type and c.status == status]

    def _claim(self, claim_id: str) -> TossClaim | None:
        return next((c for c in self.claims if c.claim_id == claim_id), None)

    async def approve_cancel(self, claim_id: str) -> Outcome:
        self.approved.append(claim_id)
        claim = self._claim(claim_id)
        if claim:
            claim.status = "APPROVED"
            order = self._order(claim.order_product_id)
            if order:
                order.status = "CANCELED_PAYMENT"
        return Outcome(claim_id, True)

    async def reject_cancel(self, claim_id: str, company: str, tracking_no: str) -> Outcome:
        self.rejected.append((claim_id, company, tracking_no))
        claim = self._claim(claim_id)
        if claim:
            claim.status = "REJECTED"
            order = self._order(claim.order_product_id)
            if order:
                order.status, order.delivery_company, order.tracking_no = "DELIVERING", company, tracking_no
        return Outcome(claim_id, True)

    async def approve_return(self, claim_id: str) -> Outcome:
        self.returns_approved.append(claim_id)
        return Outcome(claim_id, True)


class TossClient(BaseTossClient):
    """실제 토스쇼핑 API. 항목 이름은 문서 그대로 썼다."""

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        env: str = "prod",
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
        token_url: str | None = None,
    ) -> None:
        if not access_key or not secret_key:
            raise ValueError("TOSS_ACCESS_KEY / TOSS_SECRET_KEY 환경변수가 설정되어 있지 않습니다.")
        alpha = env == "alpha"
        self._access_key, self._secret_key = access_key, secret_key
        self._base_url = (base_url or (ALPHA_API if alpha else PROD_API)).rstrip("/")
        self._token_url = token_url or (ALPHA_TOKEN if alpha else PROD_TOKEN)
        self._transport = transport
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=30.0, transport=self._transport)

    async def _access_token(self) -> str:
        # 토큰은 약 1년 유효하고, 호출마다 다시 받으면 이용이 제한될 수 있어서 계속 재사용한다.
        if self._token and time.time() < self._token_expires_at - 3600:
            return self._token
        data = {
            "grant_type": "client_credentials",
            "client_id": self._access_key,
            "client_secret": self._secret_key,
            "scope": "toss-shopping-fep:write",
        }
        try:
            async with self._http() as http:
                response = await http.post(self._token_url, data=data, headers={"Accept": "application/json; charset=UTF-8"})
        except httpx.HTTPError as exc:
            raise TossError(f"토스쇼핑에 연결하지 못했습니다 (토큰 발급): {exc}") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or "access_token" not in body:
            raise TossError(f"토스쇼핑 로그인(토큰 발급)에 실패했습니다: [{response.status_code}] {str(body or response.text)[:200]}. Access Key, Secret Key와 등록한 IP를 확인하세요.")
        self._token = body["access_token"]
        self._token_expires_at = time.time() + float(body.get("expires_in", 31535999))
        return self._token

    async def _call(self, method: str, path: str, what: str, *, params=None, json=None) -> dict:
        headers = {"Authorization": f"Bearer {await self._access_token()}", "Content-Type": "application/json"}
        try:
            async with self._http() as http:
                response = await http.request(method, f"{self._base_url}{path}", headers=headers, params=params, json=json)
        except httpx.HTTPError as exc:
            raise TossError(f"토스쇼핑에 연결하지 못했습니다 ({what}): {exc}") from exc
        if response.status_code == 401:
            self._token = None
            raise TossError(f"{what}: 인증이 거절되었습니다 (401). 키와 등록한 IP를 확인하세요.")
        try:
            body = response.json()
        except ValueError:
            raise TossError(f"{what}: 알아볼 수 없는 응답입니다 ({response.status_code}) {response.text[:200]}") from None
        if not isinstance(body, dict) or body.get("resultType") != "SUCCESS":
            error = (body.get("error") or {}) if isinstance(body, dict) else {}
            raise TossError(f"{what}: [{error.get('errorCode', response.status_code)}] {error.get('reason') or ''}".strip())
        return body.get("success") or {}

    # ------------------------------------------------------------ 도움말 조회
    async def categories(self, parent_id: int | None = None) -> list[dict]:
        data = await self._call("GET", f"{API}/products/categories/children", "카테고리 조회", params={"id": parent_id} if parent_id else None)
        return data.get("items") or []

    async def category_template(self, category_id: int) -> dict:
        return await self._call("GET", f"{API}/category/{category_id}/constraint-templates", "카테고리 템플릿 조회")

    async def notice_category_codes(self) -> list[dict]:
        return (await self._call("GET", f"{API}/notices/category-codes", "고시 카테고리 조회")).get("items") or []

    async def notice_items(self, category_code: str) -> list[dict]:
        return (await self._call("GET", f"{API}/notices", "고시 항목 조회", params={"categoryCode": category_code})).get("items") or []

    async def _paged(self, path: str, what: str) -> list[dict]:
        items: list[dict] = []
        token = None
        for _ in range(20):
            params = {"size": 20, **({"nextToken": token} if token else {})}
            data = await self._call("GET", path, what, params=params)
            items.extend(data.get("items") or [])
            token = data.get("nextToken")
            if not data.get("hasNext") or not token:
                break
        return items

    async def delivery_locations(self) -> list[dict]:
        return await self._paged(f"{API}/merchants/group-delivery/delivery-location/v2", "배송비 묶음 그룹 조회")

    async def exchange_refund_locations(self) -> list[dict]:
        return await self._paged(f"{API}/merchants/group-delivery/exchange-refund-location/v2", "교환반품지 조회")

    async def delivery_companies(self) -> list[str]:
        return (await self._call("GET", f"{API}/orders/delivery-companies", "택배사 조회")).get("deliveryCompanies") or []

    # ------------------------------------------------------------ 상품
    async def register_product(self, payload: dict) -> int:
        data = await self._call("POST", f"{API}/products/v3", "상품 등록", json=payload)
        if "id" not in data:
            raise TossError("상품 등록 응답에 상품 번호가 없습니다.")
        return int(data["id"])

    # ------------------------------------------------------------ 주문
    async def fetch_orders(self, start: date, end: date) -> list[TossOrder]:
        orders: list[TossOrder] = []
        cursor = None
        for _ in range(200):
            params = {"startDate": start.isoformat(), "endDate": end.isoformat(), "limit": 50, **({"nextCursor": cursor} if cursor else {})}
            data = await self._call("GET", f"{API}/orders/v2", "주문 가져오기", params=params)
            orders.extend(_parse_order(r) for r in data.get("results") or [])
            cursor = data.get("nextCursor")
            if not cursor:
                break
        return orders

    async def dispatch(self, order_product_id: str, company: str, tracking_no: str) -> Outcome:
        await self._call(
            "PUT", f"{API}/orders/products/delivery", "송장 입력",
            json={"orderProductId": int(order_product_id), "deliveryCompany": company, "trackingNumber": tracking_no},
        )
        return Outcome(order_product_id, True)

    async def delay_shipping(self, order_product_ids: list[str], deadline: datetime, reason: str) -> list[Outcome]:
        data = await self._call(
            "PUT", f"{API}/orders/products/status", "발송 지연",
            json={
                "orderProductIds": [int(i) for i in order_product_ids],
                "status": "DELAY_SHIPPING",
                "shippingDeadlineAt": deadline.strftime("%Y-%m-%dT%H:%M:%S"),
                "delayReasonType": reason,
            },
        )
        # 일부만 실패해도 성공한 건은 바뀐다. 실패 사유는 건별로 오지 않아서 목록 전체에 붙인다.
        reasons = data.get("failedReasons") or []
        if data.get("failedCount"):
            note = "; ".join(map(str, reasons)) or "이유를 알 수 없음"
            if data.get("failedCount") >= len(order_product_ids):
                return [Outcome(i, False, note) for i in order_product_ids]
            return [Outcome(i, True, f"일부 주문이 실패했습니다: {note}") for i in order_product_ids]
        return [Outcome(i, True) for i in order_product_ids]

    # ------------------------------------------------------------ 클레임
    async def fetch_claims(self, claim_type: str, status: str = "REQUESTED") -> list[TossClaim]:
        claims: list[TossClaim] = []
        token = None
        for _ in range(20):
            params = {"type": claim_type, "status": status, "size": 100, **({"nextToken": token} if token else {})}
            data = await self._call("GET", f"{API}/claims", "클레임 조회", params=params)
            for row in data.get("items") or []:
                order = row.get("order") or {}
                claims.append(
                    TossClaim(
                        claim_id=str(row.get("id")),
                        type=str(row.get("type") or claim_type),
                        status=str(row.get("status") or status),
                        order_product_id=str(order.get("orderProductId")),
                        reason=str(row.get("requestReason") or ""),
                    )
                )
            token = data.get("nextToken")
            if not data.get("hasNext") or not token:
                break
        return claims

    async def approve_cancel(self, claim_id: str) -> Outcome:
        await self._call("POST", f"{API}/claims/{claim_id}/cancel/approval", "취소 승인")
        return Outcome(claim_id, True)

    async def reject_cancel(self, claim_id: str, company: str, tracking_no: str) -> Outcome:
        await self._call(
            "POST", f"{API}/claims/{claim_id}/cancel/rejection", "취소 거절",
            json={"deliveryCompany": company, "shippingTrackingNumber": tracking_no},
        )
        return Outcome(claim_id, True)

    async def approve_return(self, claim_id: str) -> Outcome:
        await self._call("POST", f"{API}/claims/{claim_id}/return/approval", "반품 승인")
        return Outcome(claim_id, True)


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_order(row: dict) -> TossOrder:
    """주문 내역 한 줄을 우리가 쓰는 모양으로 (항목 이름은 문서 order.md)."""
    return TossOrder(
        order_product_id=str(row.get("orderProductId")),
        order_id=str(row["orderId"]) if row.get("orderId") is not None else None,
        product_code=row.get("productManagementCode"),
        item_code=row.get("productItemManagementCode"),
        product_name=row.get("productName"),
        option_name=row.get("optionName"),
        quantity=max(_int(row.get("quantity"), 1), 1),
        price=_int(row.get("price")),
        buyer_name=row.get("ordererName"),
        receiver_name=row.get("receiverName") or "",
        # 안심번호가 아닌 실제 번호가 있으면 그것을 쓴다 (공급사 배송에 필요)
        receiver_phone=row.get("receiverRealPhone") or row.get("receiverPhone") or "",
        zipcode=row.get("zipCode"),
        address=" ".join(p for p in (row.get("address"), row.get("detailAddress")) if p),
        memo=row.get("shippingNote"),
        status=str(row.get("orderProductStatus") or ""),
        shipping_deadline=row.get("shippingDeadlineAt"),
        delivery_company=row.get("deliveryCompanyCode"),
        tracking_no=row.get("shippingTrackingNumber"),
        raw=row,
    )


_real_client: TossClient | None = None
_mock_client: MockTossClient | None = None


def get_toss_client(settings: Settings | None = None) -> BaseTossClient:
    """USE_MOCK_TOSS 에 따라 가짜 또는 실제 클라이언트를 돌려준다."""
    global _real_client, _mock_client
    settings = settings or get_settings()
    if settings.use_mock_toss:
        if _mock_client is None:
            _mock_client = MockTossClient()
        return _mock_client
    if _real_client is None:
        _real_client = TossClient(settings.toss_access_key, settings.toss_secret_key, settings.toss_env)
    return _real_client
