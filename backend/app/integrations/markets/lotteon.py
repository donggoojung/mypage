"""롯데온(LOTTE ON) Open API 클라이언트 — 상품 등록, 주문 가져오기, 송장 입력, 취소 승인.

- Mock(기본값): 가짜 응답만 돌려준다. 실제 롯데온에는 아무것도 가지 않는다.
- 실제: https://openapi.lotteon.com (인증키 + 등록된 서버 IP 필요).

근거: 롯데온 API 센터(https://api.lotteon.com) 설명서를 읽고 정리한 `agri/롯데온_설명서/` (2026-11-05).
아직 실제 키로 호출해 본 적은 없다. 설명서 안의 불일치(표와 예시가 다른 곳)는 아래 `※` 표시로 남겨 둔다.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import Settings, get_settings

LOTTEON_BASE_URL = "https://openapi.lotteon.com"
KST = timezone(timedelta(hours=9))
OK_CODE = "0000"


class LotteonError(Exception):
    """롯데온이 요청을 거절했거나 연결에 실패했을 때 (화면에 그대로 보여준다)."""


def kst_stamp(moment: datetime | None = None) -> str:
    """롯데온이 쓰는 날짜 모양 yyyymmddhhmmss (한국 시간)."""
    moment = moment or datetime.now(KST)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=KST)
    return moment.astimezone(KST).strftime("%Y%m%d%H%M%S")


@dataclass
class LotteonOrder:
    """출고지시(새 주문) 또는 회수지시(반품·교환) 한 줄."""

    od_no: str
    od_seq: int
    proc_seq: int = 1
    orgl_proc_seq: int | None = None
    clm_no: str | None = None
    od_typ_cd: str = "10"  # 10 주문 / 30 교환 / 40 반품
    dv_rtrv_dvs_cd: str = "DV"  # DV 배송 / RTRV 회수
    prgs_step: str = "11"  # 11 출고지시 / 23 회수지시
    spd_no: str | None = None
    epd_no: str | None = None  # 우리가 등록할 때 넣은 업체상품번호 (agri-상품번호)
    sitm_no: str | None = None
    eitm_no: str | None = None  # 우리가 등록할 때 넣은 업체단품번호 (agri-옵션번호)
    product_name: str | None = None
    option_name: str | None = None
    quantity: int = 1
    amount: int = 0  # 판매가(금액)
    buyer_name: str | None = None
    buyer_phone: str | None = None
    receiver_name: str = ""
    receiver_phone: str = ""
    zipcode: str | None = None
    address: str = ""
    memo: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """우리 주문표의 마켓 주문번호 (주문번호-주문순번-처리순번)."""
        return f"{self.od_no}-{self.od_seq}-{self.proc_seq}"


@dataclass
class LotteonClaimItem:
    od_seq: int
    proc_seq: int
    orgl_proc_seq: int
    step: str  # 02 요청 / 21 완료 / 22 철회
    reason_code: str = ""
    reason_text: str = ""


@dataclass
class LotteonClaim:
    od_no: str
    clm_no: str
    items: list[LotteonClaimItem] = field(default_factory=list)


@dataclass
class RegisterResult:
    epd_no: str | None
    spd_no: str | None
    ok: bool
    message: str = ""


@dataclass
class DispatchItem:
    od_no: str
    od_seq: int
    proc_seq: int
    courier_code: str
    tracking_no: str
    quantity: int


@dataclass
class Outcome:
    key: str
    ok: bool
    message: str = ""


def split_key(key: str) -> tuple[str, int, int]:
    od_no, od_seq, proc_seq = key.rsplit("-", 2)
    return od_no, int(od_seq), int(proc_seq)


class BaseLotteonClient(ABC):
    is_mock: bool = False

    @abstractmethod
    async def identity(self) -> dict:
        """연결 확인 (인증키와 서버 IP가 맞는지). 거래처 번호를 돌려준다."""

    @abstractmethod
    async def register_products(self, products: list[dict]) -> list[RegisterResult]:
        """상품 등록 (spdLst 의 각 상품)."""

    @abstractmethod
    async def fetch_orders(self, since: datetime, until: datetime) -> list[LotteonOrder]:
        """아직 연동완료를 하지 않은 출고지시·회수지시를 가져온다."""

    @abstractmethod
    async def complete_interface(self, orders: list[LotteonOrder]) -> list[Outcome]:
        """가져온 주문을 저장했다고 롯데온에 알린다 (알리면 고객의 즉시 취소가 막히고 '상품준비'가 된다)."""

    @abstractmethod
    async def check_sendable(self, od_no: str, od_seq: int, proc_seq: int) -> Outcome:
        """발송가능여부 확인 (이미 취소됐으면 실패)."""

    @abstractmethod
    async def dispatch(self, items: list[DispatchItem]) -> list[Outcome]:
        """송장 입력 (발송완료 통보)."""

    @abstractmethod
    async def fetch_cancel_claims(self, since: datetime, until: datetime, od_no: str | None = None) -> list[LotteonClaim]:
        """고객의 취소 요청·완료·철회 목록."""

    @abstractmethod
    async def approve_cancel(self, claim: LotteonClaim, reason_code: str) -> Outcome:
        """취소 요청 승인."""

    @abstractmethod
    async def hold_cancel(self, claim: LotteonClaim, reason_code: str) -> Outcome:
        """취소 요청 거부 (901 이미 발송, 902 협의 후 거부, 903 취소 어려운 상품)."""


class MockLotteonClient(BaseLotteonClient):
    """키 없이 개발·시험하는 가짜 클라이언트. 호출 내용을 기록해 둔다."""

    is_mock = True

    def __init__(self) -> None:
        self.registered: list[dict] = []
        self.orders: list[LotteonOrder] = []
        self.completed: list[str] = []  # 연동완료를 보낸 주문 key
        self.dispatched: list[DispatchItem] = []
        self.claims: list[LotteonClaim] = []
        self.approved: list[str] = []  # 승인한 클레임번호
        self.held: list[tuple[str, str]] = []
        self.unsendable: set[str] = set()
        self.fail_dispatch_for: set[str] = set()
        self._counter = 0

    async def identity(self) -> dict:
        return {"trGrpCd": "SR", "trDvsCd": "SR", "trNo": "LOMOCK0001", "trNm": "연습용 거래처"}

    async def register_products(self, products: list[dict]) -> list[RegisterResult]:
        results = []
        for product in products:
            self.registered.append(product)
            self._counter += 1
            results.append(RegisterResult(product.get("epdNo"), f"LO{100000 + self._counter}", True))
        return results

    def add_order(self, **fields) -> LotteonOrder:
        """연습용으로 '고객이 주문한 것처럼' 가짜 출고지시를 만든다."""
        token = uuid.uuid4().hex[:10].upper()
        data = {"od_no": f"MOCK-{token}", "od_seq": 1}
        data.update(fields)
        order = LotteonOrder(**data)
        self.orders.append(order)
        return order

    async def fetch_orders(self, since: datetime, until: datetime) -> list[LotteonOrder]:
        return [o for o in self.orders if o.key not in self.completed]

    async def complete_interface(self, orders: list[LotteonOrder]) -> list[Outcome]:
        self.completed.extend(o.key for o in orders)
        return [Outcome(o.key, True) for o in orders]

    async def check_sendable(self, od_no: str, od_seq: int, proc_seq: int) -> Outcome:
        key = f"{od_no}-{od_seq}-{proc_seq}"
        if key in self.unsendable:
            return Outcome(key, False, "발송처리가 불가능합니다 (연습용)")
        return Outcome(key, True)

    async def dispatch(self, items: list[DispatchItem]) -> list[Outcome]:
        results = []
        for item in items:
            key = f"{item.od_no}-{item.od_seq}-{item.proc_seq}"
            if key in self.fail_dispatch_for:
                results.append(Outcome(key, False, "연습용 실패"))
                continue
            self.dispatched.append(item)
            results.append(Outcome(key, True))
        return results

    def add_cancel_request(self, order: LotteonOrder, reason_code: str = "106") -> LotteonClaim:
        """연습용으로 고객이 취소를 요청한 것처럼 만든다."""
        claim = LotteonClaim(
            od_no=order.od_no,
            clm_no=f"MOCKCLM{uuid.uuid4().hex[:8].upper()}",
            items=[LotteonClaimItem(order.od_seq, order.proc_seq + 1, order.proc_seq, "02", reason_code, "구매의사 없어짐")],
        )
        self.claims.append(claim)
        return claim

    async def fetch_cancel_claims(self, since: datetime, until: datetime, od_no: str | None = None) -> list[LotteonClaim]:
        return [c for c in self.claims if od_no is None or c.od_no == od_no]

    async def approve_cancel(self, claim: LotteonClaim, reason_code: str) -> Outcome:
        self.approved.append(claim.clm_no)
        for stored in self.claims:
            if stored.clm_no == claim.clm_no:
                for item in stored.items:
                    item.step = "21"
        return Outcome(claim.clm_no, True)

    async def hold_cancel(self, claim: LotteonClaim, reason_code: str) -> Outcome:
        self.held.append((claim.clm_no, reason_code))
        return Outcome(claim.clm_no, True)


class LotteonClient(BaseLotteonClient):
    """실제 롯데온 Open API. 항목 이름은 설명서 그대로 썼다."""

    ORDER_WINDOW = timedelta(days=1)  # 출고지시 조회는 1일을 넘길 수 없다
    CLAIM_WINDOW = timedelta(days=30)  # 취소요청 조회는 30일을 넘길 수 없다

    def __init__(
        self,
        api_key: str,
        base_url: str = LOTTEON_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("LOTTEON_API_KEY 환경변수가 설정되어 있지 않습니다.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Accept-Language": "ko",
            "X-Timezone": "GMT+09:00",
            "Content-Type": "application/json",
        }

    async def _call(self, method: str, path: str, what: str, *, json=None, params=None, accept_codes=(OK_CODE,)) -> dict:
        try:
            async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as http:
                response = await http.request(method, f"{self._base_url}{path}", headers=self._headers(), json=json, params=params)
        except httpx.HTTPError as exc:
            raise LotteonError(f"롯데온에 연결하지 못했습니다 ({what}): {exc}") from exc
        if response.status_code == 401:
            raise LotteonError(f"{what}: 인증키가 등록되지 않았거나 만료되었습니다 (401). 판매자센터 OpenAPI관리에서 인증키를 확인하세요.")
        if response.status_code == 403:
            raise LotteonError(f"{what}: 접근이 거부되었습니다 (403). 이 컴퓨터의 인터넷 주소(IP)가 판매자센터 OpenAPI관리에 등록되어 있는지 확인하세요.")
        if response.status_code == 429:
            raise LotteonError(f"{what}: 호출 횟수를 넘었습니다 (429). 잠시 뒤에 다시 해 주세요.")
        try:
            body = response.json()
        except ValueError:
            raise LotteonError(f"{what}: 알아볼 수 없는 응답입니다 ({response.status_code}) {response.text[:200]}") from None
        if response.status_code != 200 or not isinstance(body, dict):
            raise LotteonError(f"{what}: 서버 오류 ({response.status_code}) {str(body)[:200]}")
        code = str(body.get("returnCode", ""))
        if code not in accept_codes:
            raise LotteonError(f"{what}: [{code}] {body.get('message') or ''}".strip())
        return body

    # ------------------------------------------------------------ 연결 확인
    async def identity(self) -> dict:
        body = await self._call("GET", "/v1/openapi/common/v1/identity", "연결 확인")
        return body.get("data") or {}

    # ------------------------------------------------------------ 상품
    async def register_products(self, products: list[dict]) -> list[RegisterResult]:
        body = await self._call(
            "POST", "/v1/openapi/product/v1/product/registration/request", "상품 등록", json={"spdLst": products}
        )
        results = []
        for row in body.get("data") or []:
            ok = str(row.get("resultCode")) == OK_CODE
            results.append(RegisterResult(row.get("epdNo"), row.get("spdNo"), ok, str(row.get("resultMessage") or "")))
        return results

    # ------------------------------------------------------------ 주문
    async def fetch_orders(self, since: datetime, until: datetime) -> list[LotteonOrder]:
        orders: list[LotteonOrder] = []
        start = since
        while start < until:
            end = min(start + self.ORDER_WINDOW - timedelta(seconds=1), until)
            body = await self._call(
                "POST",
                "/v1/openapi/delivery/v1/SellerDeliveryOrdersSearch",
                "주문 가져오기",
                json={"srchStrtDt": kst_stamp(start), "srchEndDt": kst_stamp(end), "odNo": "", "odPrgsStepCd": "", "odTypCd": "", "ifCplYN": "N"},
            )
            data = body.get("data")
            # ※ 설명서의 표는 최상위 deliveryOrderList, 예시는 data.deliveryOrderList 라서 둘 다 받아 준다.
            rows = (data.get("deliveryOrderList") if isinstance(data, dict) else None) or body.get("deliveryOrderList") or []
            orders.extend(_parse_order(r) for r in rows)
            start = end + timedelta(seconds=1)
        return orders

    async def complete_interface(self, orders: list[LotteonOrder]) -> list[Outcome]:
        if not orders:
            return []
        payload = {
            "ifCompleteList": [
                {
                    "dvRtrvDvsCd": o.dv_rtrv_dvs_cd,
                    "odNo": o.od_no,
                    "odSeq": o.od_seq,
                    "procSeq": o.proc_seq,
                    "orglProcSeq": o.orgl_proc_seq if o.orgl_proc_seq is not None else "",
                    "clmNo": o.clm_no or "",
                    "ifCplYN": "Y",
                    "ifFlRsnCnts": "",
                }
                for o in orders
            ]
        }
        await self._call("POST", "/v1/openapi/delivery/v1/SellerIfCompleteInform", "주문 연동완료 통보", json=payload)
        return [Outcome(o.key, True) for o in orders]

    async def check_sendable(self, od_no: str, od_seq: int, proc_seq: int) -> Outcome:
        key = f"{od_no}-{od_seq}-{proc_seq}"
        body = await self._call(
            "GET",
            "/v1/openapi/claim/v1/deliveryClaimExtApi/getSendPossibleYn",
            "발송가능여부 확인",
            params={"odNo": od_no, "odSeq": od_seq, "procSeq": proc_seq},
            accept_codes=(OK_CODE, "6001"),
        )
        if str(body.get("returnCode")) == "6001":
            return Outcome(key, False, "롯데온에서 발송처리가 불가능한 주문입니다 (이미 취소되었을 수 있습니다).")
        return Outcome(key, True)

    async def dispatch(self, items: list[DispatchItem]) -> list[Outcome]:
        if not items:
            return []
        now = kst_stamp()
        payload = {
            "deliveryProgressStateList": [
                {
                    "dvRtrvDvsCd": "DV",
                    "odNo": i.od_no,
                    "odSeq": i.od_seq,
                    "procSeq": i.proc_seq,
                    "orglProcSeq": "",
                    "clmNo": "",
                    "odPrgsStepCd": "13",  # 발송완료
                    "dvTrcStatDttm": now,
                    "invcNbr": 1,
                    "dvCoCd": i.courier_code,
                    "invcNoList": [i.tracking_no],
                    "slQty": i.quantity,
                }
                for i in items
            ]
        }
        body = await self._call("POST", "/v1/openapi/delivery/v2/SellerDeliveryProgressStateInform", "송장 입력", json=payload)
        # returnCode 가 0000 이어도 건별 실패가 data.failList 에 들어온다.
        failed = {
            f"{f.get('odNo')}-{f.get('odSeq')}-{f.get('procSeq')}": f"[{f.get('rsltCd')}] {f.get('rsltMsg') or ''}".strip()
            for f in ((body.get("data") or {}).get("failList") or [])
        }
        return [
            Outcome(k, k not in failed, failed.get(k, ""))
            for k in (f"{i.od_no}-{i.od_seq}-{i.proc_seq}" for i in items)
        ]

    # ------------------------------------------------------------ 취소
    async def fetch_cancel_claims(self, since: datetime, until: datetime, od_no: str | None = None) -> list[LotteonClaim]:
        claims: dict[str, LotteonClaim] = {}
        start = since
        while start < until:
            end = min(start + self.CLAIM_WINDOW - timedelta(seconds=1), until)
            payload = {"srchStrtDttm": kst_stamp(start), "srchEndDttm": kst_stamp(end)}
            if od_no:
                payload["odNo"] = od_no
            body = await self._call(
                "POST",
                "/v1/openapi/claim/v1/cancellationOpenApi/getCancellationRequestAndComplateList",
                "취소 요청 조회",
                json=payload,
                accept_codes=(OK_CODE, "3000"),  # 3000 = 조회내역이 없습니다
            )
            for row in body.get("data") or []:
                claim = claims.setdefault(
                    str(row.get("clmNo") or row.get("cmNo")), LotteonClaim(str(row.get("odNo")), str(row.get("clmNo") or row.get("cmNo")))
                )
                for item in row.get("itemList") or []:
                    claim.items.append(
                        LotteonClaimItem(
                            od_seq=int(item["odSeq"]),
                            proc_seq=int(item["procSeq"]),
                            orgl_proc_seq=int(item.get("orglProcSeq") or item["procSeq"]),
                            step=str(item.get("odPrgsStepCd") or ""),
                            reason_code=str(item.get("clmRsnCd") or ""),
                            reason_text=str(item.get("clmRsnCnts") or ""),
                        )
                    )
            start = end + timedelta(seconds=1)
        return list(claims.values())

    @staticmethod
    def _claim_payload(claim: LotteonClaim, reason_code: str) -> dict:
        return {
            "odNo": claim.od_no,
            "clmNo": claim.clm_no,
            "itemList": [
                {"odSeq": i.od_seq, "procSeq": i.proc_seq, "orglProcSeq": i.orgl_proc_seq, "slrRsnCd": reason_code}
                for i in claim.items
            ],
        }

    async def approve_cancel(self, claim: LotteonClaim, reason_code: str) -> Outcome:
        await self._call(
            "POST", "/v1/openapi/claim/v1/cancellationOpenApi/cnclRequestApproval", "취소 승인", json=self._claim_payload(claim, reason_code)
        )
        return Outcome(claim.clm_no, True)

    async def hold_cancel(self, claim: LotteonClaim, reason_code: str) -> Outcome:
        # ※ 이름은 "거부"인데 주소 끝이 cnclRequestHold(보류)다. 설명서 그대로.
        await self._call(
            "POST", "/v1/openapi/claim/v1/cancellationOpenApi/cnclRequestHold", "취소 거부", json=self._claim_payload(claim, reason_code)
        )
        return Outcome(claim.clm_no, True)


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_order(row: dict) -> LotteonOrder:
    """출고/회수지시 한 줄을 우리가 쓰는 모양으로 (항목 이름은 설명서 209번)."""
    road = " ".join(p for p in (row.get("dvpStnmZipAddr"), row.get("dvpStnmDtlAddr")) if p)
    lot = " ".join(p for p in (row.get("dvpJbZipAddr"), row.get("dvpJbDtlAddr")) if p)
    orgl = row.get("orglProcSeq")
    return LotteonOrder(
        od_no=str(row.get("odNo")),
        od_seq=_int(row.get("odSeq"), 1),
        proc_seq=_int(row.get("procSeq"), 1),
        orgl_proc_seq=_int(orgl) if orgl not in (None, "") else None,
        clm_no=row.get("clmNo") or None,
        od_typ_cd=str(row.get("odTypCd") or "10"),
        dv_rtrv_dvs_cd=str(row.get("dvRtrvDvsCd") or "DV"),
        prgs_step=str(row.get("odPrgsStepCd") or "11"),
        spd_no=row.get("spdNo"),
        epd_no=row.get("epdNo"),
        sitm_no=row.get("sitmNo"),
        eitm_no=row.get("eitmNo"),
        product_name=row.get("spdNm"),
        option_name=row.get("sitmNm"),
        quantity=max(_int(row.get("odQty"), 1), 1),
        amount=_int(row.get("slAmt")),
        buyer_name=row.get("odrNm"),
        buyer_phone=row.get("mphnNo"),
        receiver_name=row.get("dvpCustNm") or "",
        receiver_phone=row.get("dvpMphnNo") or row.get("dvpTelNo") or "",
        zipcode=row.get("dvpZipNo"),
        address=road or lot,
        memo=row.get("dvMsg"),
        raw=row,
    )


_real_client: LotteonClient | None = None
_mock_client: MockLotteonClient | None = None


def get_lotteon_client(settings: Settings | None = None) -> BaseLotteonClient:
    """USE_MOCK_LOTTEON 에 따라 가짜 또는 실제 클라이언트를 돌려준다."""
    global _real_client, _mock_client
    settings = settings or get_settings()
    if settings.use_mock_lotteon:
        if _mock_client is None:
            _mock_client = MockLotteonClient()
        return _mock_client
    if _real_client is None:
        _real_client = LotteonClient(settings.lotteon_api_key)
    return _real_client
