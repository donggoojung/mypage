"""쿠팡 WING Open API(마켓플레이스 판매자 오픈API) 연동 (PRD 4차 지시).

주의 — 실API 미검증: 이 코드는 coupang.com/api-gateway.coupang.com 아웃바운드 접속이
차단된 개발 환경에서, 쿠팡 공식 문서(https://developers.coupangcorp.com)에 대한 기억을
바탕으로 작성됐다. HMAC 서명 생성 로직은 오래 안정적으로 유지되어온 방식이라 신뢰도가
높지만, 상품 등록 페이로드의 정확한 필드명/필수값 구성은 실제 API로 재검증이 필요하다.
반드시 실제 키가 준비되면 `USE_MOCK_MARKETS=false` 로 바꾸고 소량 테스트 등록부터
진행해서 검증한 뒤, 실패하는 필드가 있으면 실제 에러 메시지를 참고해 payload를 조정한다.

인증 방식(HMAC-SHA256)은 쿠팡 WING 오픈API 공통 스펙:
    message = signed_date + method + path + query
    signature = HMAC-SHA256(secret_key, message).hexdigest()
    Authorization: CEA algorithm=HmacSHA256, access-key={access_key},
                   signed-date={signed_date}, signature={signature}
"""

import asyncio
import hashlib
import hmac
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.enums import ListingStatus, MarketType
from app.models.generated_asset import GeneratedAsset
from app.models.market_listing import MarketListing
from app.models.master_product import MasterProduct

COUPANG_API_HOST = "https://api-gateway.coupang.com"
PRODUCT_REGISTRATION_PATH = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
# 2026-09-21 실계정 테스트로 확인됨: 출고지 조회 경로는 openapi가 아니라
# marketplace_openapi이고, vendorId를 경로에 안 넣는다(API 키로 자동 식별됨) — 처음에
# 추측했던 경로(v4/vendors/{id}/shipping-place/list)는 404였다.
SHIPPING_PLACE_LIST_PATH = "/v2/providers/marketplace_openapi/apis/api/v1/vendor/shipping-place/outbound"
# 2026-09-21 실계정 테스트로 확인됨: v4 경로는 200 OK를 반환하지만 WING 화면에는
# 분명히 "사용중" 상태로 있는 반품지가 빈 배열([])로 나왔다 — v4가 오래된/캐시된
# 버전일 가능성이 있어 v5로 변경해본다 (v5가 "공식 경로"라는 자료도 있었음).
RETURN_SHIPPING_CENTER_LIST_PATH = "/v2/providers/openapi/apis/api/v5/vendors/{vendor_id}/returnShippingCenters"
ORDER_SHEETS_PATH = "/v2/providers/openapi/apis/api/v4/vendors/{vendor_id}/ordersheets"
CATEGORY_PREDICTION_PATH = "/v2/providers/openapi/apis/api/v1/categorization/predict"
# 2026-09-22 웹검색으로 실제 문서/오픈소스 구현체 확인됨: 카테고리별로 요구하는
# 상품정보제공고시(신발/의류/기타재화 등) 항목이 다르다 — displayCategoryCode로 이
# API를 호출해 그 카테고리가 실제로 요구하는 항목 목록을 받아와야 한다.
CATEGORY_METADATA_PATH = "/v2/providers/seller_api/apis/api/v1/marketplace/meta/category-related-metas/display-category-codes/{display_category_code}"

# 카테고리 메타정보 조회가 실패했을 때(권한 문제 등)만 쓰는 폴백 — 실API 미검증인 예시값이라
# 부정확할 수 있다. 정상 흐름에서는 항상 위 API가 돌려주는 실제 값을 우선 사용한다.
DEFAULT_NOTICE_CATEGORY = "신발"
DEFAULT_NOTICE_DETAIL_KEYS = ("소재", "색상", "치수", "제조자(수입자)", "제조국", "세탁방법 및 취급시 주의사항")


class CoupangRegistrationError(RuntimeError):
    """쿠팡 상품 등록 API 호출/응답 처리 중 발생한 오류."""


def _generate_hmac_signature(method: str, path: str, secret_key: str, query: str = "") -> tuple[str, str]:
    """쿠팡 WING 오픈API 표준 HMAC-SHA256 서명을 생성한다.

    2026-09-21 실계정 테스트로 확인됨: 서명용 메시지에는 쿼리스트링을 그대로 이어붙이되
    맨 앞의 "?"는 빼야 한다(실제 요청 URL에는 "?"를 붙인다) — 이 함수를 호출하는 쪽에서는
    URL용으로 "?"가 붙은 query를 그대로 넘겨도 되도록, 여기서 lstrip으로 제거한다.
    "?"를 포함한 채로 서명하면 쿠팡 서버가 401 Unauthorized로 거부한다.

    Returns: (signed_date, signature_hex)
    """
    now = datetime.now(UTC)
    signed_date = now.strftime("%y%m%d") + "T" + now.strftime("%H%M%S") + "Z"
    message = signed_date + method.upper() + path + query.lstrip("?")
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return signed_date, signature


def _build_authorization_header(method: str, path: str, access_key: str, secret_key: str, query: str = "") -> dict:
    signed_date, signature = _generate_hmac_signature(method, path, secret_key, query)
    authorization = (
        f"CEA algorithm=HmacSHA256, access-key={access_key}, "
        f"signed-date={signed_date}, signature={signature}"
    )
    return {"Authorization": authorization, "Content-Type": "application/json;charset=UTF-8"}


@dataclass
class CoupangProductInput:
    """쿠팡 상품 등록 페이로드 빌더에 필요한 정규화된 입력 데이터."""

    style_code: str
    brand_name: str
    product_name: str
    display_category_code: int
    selling_price: Decimal
    vendor_id: str
    # WING 로그인 아이디(이메일/로그인ID) — 코드가 추측할 수 없어 설정(COUPANG_VENDOR_USER_ID)에서
    # 받아온다. 2026-09-21 실API 검증됨: 이 값이 없으면 "vendorUserId 값을 확인해 주세요"로 거부된다.
    vendor_user_id: str
    thumbnail_image_url: str
    detail_image_url: str
    size_stock: dict[str, dict]  # {"250": {"stock": 5, "is_sold_out": False}, ...}
    specs: dict[str, str] = field(default_factory=dict)  # {"소재": "...", "제조국": "...", ...}
    original_price: Decimal | None = None  # None이면 selling_price와 동일하게 처리(할인 없음)
    manufacturer: str = ""
    # False(기본값, 안전) = 임시저장만 하고 실제 판매 심사요청은 보내지 않는다.
    # True로 바꿔야만 쿠팡에 승인요청이 실제로 들어간다 — 실계정 첫 테스트는 반드시 False로.
    request_approval: bool = False
    # 카테고리별로 요구하는 상품정보제공고시 항목이 달라, 정상 흐름에서는
    # resolve_notice_info()가 카테고리 메타정보 API로 조회한 실제 값을 채운다.
    # 조회 실패 시에만 아래 기본값(신발 카테고리 예시, 실API 미검증)을 그대로 쓴다.
    notice_category_name: str = DEFAULT_NOTICE_CATEGORY
    notice_detail_keys: tuple[str, ...] = DEFAULT_NOTICE_DETAIL_KEYS


def build_seller_product_payload(data: CoupangProductInput, seller_info: dict) -> dict:
    """PRD 3장 산출물(AI 이미지) + 4장 마진 엔진 판매가 + 사이즈 재고를 쿠팡
    '상품 등록 API' 페이로드로 변환한다.

    `seller_info`는 판매자 계정 고유 설정(반품지코드, 출고지코드 등 — WING 판매자센터에서
    확인)으로, 코드가 추측할 수 없어 외부에서 주입받는다.
    """
    if not data.size_stock:
        raise ValueError("size_stock이 비어있어 쿠팡 옵션(items)을 만들 수 없습니다.")

    original_price = data.original_price if data.original_price is not None else data.selling_price
    notices = [
        {"noticeCategoryName": data.notice_category_name, "noticeCategoryDetailName": key, "content": data.specs.get(key, "상품 상세 참조")}
        for key in data.notice_detail_keys
    ]
    print(f"  [진단] 제출할 notices: {notices}", flush=True)
    attributes_base = [{"attributeTypeName": key, "attributeValueName": value} for key, value in data.specs.items()]

    items = []
    for size, stock_info in data.size_stock.items():
        if stock_info.get("is_sold_out"):
            continue  # 품절 사이즈는 옵션으로 노출하지 않는다.
        items.append(
            {
                "itemName": size,
                "originalPrice": int(original_price),
                "salePrice": int(data.selling_price),
                "maximumBuyCount": 999,
                "maximumBuyForPerson": 0,
                # 2026-09-21 실API 검증됨: 0을 넣으면 "최소 1 이상이어야 한다"고 거부된다.
                "maximumBuyForPersonPeriod": 1,
                "outboundShippingTimeDay": 2,
                "unitCount": 1,
                "adultOnly": "EVERYONE",
                "taxType": "TAX",
                "parallelImported": "NOT_PARALLEL_IMPORTED",
                "overseasPurchased": "NOT_OVERSEAS_PURCHASED",
                "pccNeeded": False,
                "externalVendorSku": f"{data.style_code}-{size}",
                "emptyBarcode": True,
                "emptyBarcodeReason": "상품 특성상 바코드 없음",
                "modelNo": data.style_code,
                "searchTags": [data.brand_name, data.product_name],
                "images": [
                    {"imageOrder": 0, "imageType": "REPRESENTATION", "cdnPath": data.thumbnail_image_url},
                    {"imageOrder": 1, "imageType": "DETAIL", "cdnPath": data.detail_image_url},
                ],
                "notices": notices,
                "attributes": [*attributes_base, {"attributeTypeName": "사이즈", "attributeValueName": size}],
                "contents": [
                    {
                        "contentsType": "HTML",
                        "contentDetails": [{"content": data.detail_image_url, "detailType": "IMAGE"}],
                    }
                ],
            }
        )

    if not items:
        raise ValueError("모든 사이즈가 품절 상태라 등록 가능한 옵션(items)이 없습니다.")

    now = datetime.now(UTC)
    return {
        "displayCategoryCode": data.display_category_code,
        "sellerProductName": f"{data.brand_name} {data.product_name} {data.style_code}",
        "vendorId": data.vendor_id,
        "saleStartedAt": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "saleEndedAt": now.replace(year=now.year + 2).strftime("%Y-%m-%dT%H:%M:%S"),
        "displayProductName": f"{data.brand_name} {data.product_name}",
        "brand": data.brand_name,
        "generalProductName": data.product_name,
        # 2026-09-21 실API 검증됨: "SEQUENCE"가 아니라 "SEQUENCIAL"(오타처럼 보이지만
        # 쿠팡 API가 실제로 기대하는 철자)이어야 한다.
        "deliveryMethod": "SEQUENCIAL",
        "vendorUserId": data.vendor_user_id,
        "deliveryCompanyCode": seller_info.get("delivery_company_code", "CJGLS"),
        "deliveryChargeType": "FREE",
        "deliveryCharge": 0,
        "freeShipOverAmount": 0,
        "deliveryChargeOnReturn": 6000,
        "remoteAreaDeliverable": "N",
        "unionDeliveryType": "NOT_UNION_DELIVERY",
        "returnCenterCode": seller_info.get("return_center_code", ""),
        "returnChargeName": seller_info.get("return_charge_name", ""),
        "companyContactNumber": seller_info.get("company_contact_number", ""),
        "returnZipCode": seller_info.get("return_zip_code", ""),
        "returnAddress": seller_info.get("return_address", ""),
        "returnAddressDetail": seller_info.get("return_address_detail", ""),
        "returnCharge": 6000,
        "outboundShippingPlaceCode": seller_info.get("outbound_shipping_place_code", ""),
        # False면 임시저장 상태로만 등록되고 실제 판매 심사요청은 나가지 않는다 (안전한 실계정 테스트용).
        "requested": data.request_approval,
        "items": items,
    }


class CoupangWingClient:
    """쿠팡 WING Open API 클라이언트 — Mock/Real 겸용."""

    def __init__(self, settings: Settings | None = None, use_mock: bool | None = None):
        self._settings = settings or get_settings()
        self._use_mock = self._settings.use_mock_markets if use_mock is None else use_mock
        if not self._use_mock:
            if not (self._settings.coupang_access_key and self._settings.coupang_secret_key):
                raise ValueError("COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 환경변수가 설정되어 있지 않습니다.")

    async def register_product(self, payload: dict) -> dict:
        """쿠팡 상품 등록 API를 호출(또는 Mock 검증)하고 응답을 반환한다."""
        self._validate_payload(payload)

        if self._use_mock:
            return self._mock_register(payload)
        return await self._real_register(payload)

    @staticmethod
    def _validate_payload(payload: dict) -> None:
        """실제 API 호출 전, 필수 필드가 빠지지 않았는지 최소한으로 검증한다."""
        required_top_level = [
            "displayCategoryCode",
            "sellerProductName",
            "vendorId",
            "items",
        ]
        missing = [key for key in required_top_level if not payload.get(key)]
        if missing:
            raise CoupangRegistrationError(f"필수 필드 누락: {missing}")

        for idx, item in enumerate(payload["items"]):
            required_item_fields = ["itemName", "salePrice", "originalPrice", "externalVendorSku"]
            missing_item = [key for key in required_item_fields if key not in item]
            if missing_item:
                raise CoupangRegistrationError(f"items[{idx}] 필수 필드 누락: {missing_item}")
            if not item.get("images"):
                raise CoupangRegistrationError(f"items[{idx}]에 images가 없습니다 (대표/상세 이미지 필수).")

    def _mock_register(self, payload: dict) -> dict:
        fake_seller_product_id = random.randint(10_000_000, 99_999_999)
        return {
            "code": "SUCCESS",
            "message": "(Mock) 상품이 성공적으로 등록되었습니다.",
            "data": fake_seller_product_id,
        }

    async def _real_register(self, payload: dict) -> dict:
        headers = _build_authorization_header(
            method="POST",
            path=PRODUCT_REGISTRATION_PATH,
            access_key=self._settings.coupang_access_key,
            secret_key=self._settings.coupang_secret_key,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{COUPANG_API_HOST}{PRODUCT_REGISTRATION_PATH}", headers=headers, json=payload
            )
            response.raise_for_status()
            return response.json()

    async def fetch_outbound_shipping_places(self, vendor_id: str) -> list[dict]:
        """계정에 등록된 출고지 목록을 조회한다 (상품 등록 시 outboundShippingPlaceCode에 사용)."""
        if self._use_mock:
            return [
                {"outboundShippingPlaceCode": 12345678, "shippingPlaceName": "(Mock) 본사 출고지", "usable": True},
            ]
        return await self._real_fetch_shipping_places(vendor_id)

    async def fetch_return_shipping_centers(self, vendor_id: str) -> list[dict]:
        """계정에 등록된 반품지 목록을 조회한다 (상품 등록 시 returnCenterCode/반품지 주소에 사용)."""
        if self._use_mock:
            return [
                {
                    "returnCenterCode": "1000274596",
                    "shippingPlaceName": "(Mock) 본사 반품지",
                    "usable": True,
                    "placeAddresses": [
                        {
                            "returnZipCode": "12345",
                            "returnAddress1": "서울시 강남구 테헤란로 1",
                            "returnAddress2": "101호",
                            "companyContactNumber": "01012345678",
                        }
                    ],
                },
            ]
        return await self._real_fetch_return_shipping_centers(vendor_id)

    async def _real_fetch_shipping_places(self, vendor_id: str) -> list[dict]:
        # 이 엔드포인트는 vendorId를 경로에 넣지 않는다 — API 키(액세스/시크릿)로 벤더가
        # 자동 식별된다. vendor_id 인자는 다른 실제 API(반품지 조회 등)와 시그니처를
        # 맞추기 위해 남겨뒀을 뿐 여기서는 쓰지 않는다.
        path = SHIPPING_PLACE_LIST_PATH
        query = "?pageNum=1&pageSize=50"
        headers = _build_authorization_header(
            method="GET",
            path=path,
            access_key=self._settings.coupang_access_key,
            secret_key=self._settings.coupang_secret_key,
            query=query,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{COUPANG_API_HOST}{path}{query}", headers=headers)
            response.raise_for_status()
            return response.json().get("content", [])

    async def _real_fetch_return_shipping_centers(self, vendor_id: str) -> list[dict]:
        path = RETURN_SHIPPING_CENTER_LIST_PATH.format(vendor_id=vendor_id)
        query = "?pageNum=1&pageSize=50"
        headers = _build_authorization_header(
            method="GET",
            path=path,
            access_key=self._settings.coupang_access_key,
            secret_key=self._settings.coupang_secret_key,
            query=query,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{COUPANG_API_HOST}{path}{query}", headers=headers)
            response.raise_for_status()
            return response.json().get("content", [])


    async def fetch_paid_order_sheets(self, vendor_id: str) -> list[dict]:
        """결제 완료(발송 대상) 상태의 신규 주문 목록을 조회한다 (PRD 5.1 주문 감지).

        각 주문 항목의 `externalVendorSku`는 상품 등록 시 우리가 `{style_code}-{size}`
        형식으로 직접 채워넣은 값이라(build_seller_product_payload 참고), 이 값을 역으로
        파싱하면 별도 매핑 테이블 없이 주문 → 마스터상품/사이즈를 바로 연결할 수 있다.
        """
        if self._use_mock:
            return self._mock_paid_order_sheets()
        return await self._real_fetch_paid_order_sheets(vendor_id)

    @staticmethod
    def _mock_paid_order_sheets() -> list[dict]:
        return [
            {
                "orderId": 700000001,
                "paidAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                "receiver": {
                    "name": "홍길동",
                    "safeNumber": "0501-1234-5678",
                    "addr1": "서울시 강남구 테헤란로 1",
                    "addr2": "101호",
                },
                "orderItems": [
                    {
                        "externalVendorSku": "CW2288-111-250",
                        "shippingCount": 1,
                        "salesPrice": 192834,
                    }
                ],
            }
        ]

    async def _real_fetch_paid_order_sheets(self, vendor_id: str) -> list[dict]:
        path = ORDER_SHEETS_PATH.format(vendor_id=vendor_id)
        # status=INSTRUCT: 결제 완료 후 상품 준비(발송) 대기 중인 신규 주문만 조회 (실API 미검증).
        query = "?status=INSTRUCT"
        headers = _build_authorization_header(
            method="GET",
            path=path,
            access_key=self._settings.coupang_access_key,
            secret_key=self._settings.coupang_secret_key,
            query=query,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{COUPANG_API_HOST}{path}{query}", headers=headers)
            response.raise_for_status()
            return response.json().get("data", [])

    async def predict_category(self, product_name: str) -> dict:
        """상품명으로 쿠팡 전시카테고리를 자동 추천받는다 (PRD 4장 등록 준비).

        WING 판매자센터에서 사용자가 직접 카테고리를 찾아 코드를 입력하지 않아도 되게
        하려는 목적 — 대신 상품명(브랜드+제품명)을 넣으면 쿠팡이 어울리는 전시카테고리
        코드를 추천해준다. 실API 미검증 — 파일 상단 설명 참고.
        """
        if self._use_mock:
            return self._mock_predict_category(product_name)
        return await self._real_predict_category(product_name)

    @staticmethod
    def _mock_predict_category(product_name: str) -> dict:
        return {
            "predictedCategoryId": 56137,
            "predictedCategoryName": "(Mock) 스니커즈/운동화",
            "autoCategorizationServiceApplicable": True,
        }

    async def _real_predict_category(self, product_name: str) -> dict:
        headers = _build_authorization_header(
            method="POST",
            path=CATEGORY_PREDICTION_PATH,
            access_key=self._settings.coupang_access_key,
            secret_key=self._settings.coupang_secret_key,
        )
        payload = {"productName": product_name}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{COUPANG_API_HOST}{CATEGORY_PREDICTION_PATH}", headers=headers, json=payload
            )
            response.raise_for_status()
            body = response.json()
            return body.get("data", body)

    async def fetch_category_metadata(self, display_category_code: int) -> dict:
        """전시카테고리가 실제로 요구하는 상품정보제공고시/옵션 등의 메타정보를 조회한다.

        2026-09-22 웹검색으로 확인됨: 카테고리마다 요구하는 고시정보 항목(신발/의류/기타재화
        등)이 다르고, 신발 카테고리라고 항상 "소재/색상/치수/제조자.../제조국/세탁방법..."을
        요구하는 게 아니다 — 이 API로 실제 그 카테고리가 요구하는 항목을 받아와야 한다.
        """
        if self._use_mock:
            return self._mock_category_metadata()
        return await self._real_fetch_category_metadata(display_category_code)

    @staticmethod
    def _mock_category_metadata() -> dict:
        return {
            "noticeCategories": [
                {
                    "noticeCategoryName": "(Mock) 신발",
                    "noticeCategoryDetailNames": [
                        {"name": "소재", "required": "MANDATORY"},
                        {"name": "색상", "required": "MANDATORY"},
                        {"name": "치수", "required": "MANDATORY"},
                        {"name": "제조자(수입자)", "required": "MANDATORY"},
                        {"name": "제조국", "required": "MANDATORY"},
                        {"name": "세탁방법 및 취급시 주의사항", "required": "MANDATORY"},
                    ],
                }
            ]
        }

    async def _real_fetch_category_metadata(self, display_category_code: int) -> dict:
        path = CATEGORY_METADATA_PATH.format(display_category_code=display_category_code)
        headers = _build_authorization_header(
            method="GET",
            path=path,
            access_key=self._settings.coupang_access_key,
            secret_key=self._settings.coupang_secret_key,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{COUPANG_API_HOST}{path}", headers=headers)
            response.raise_for_status()
            body = response.json()
            return body.get("data", body)


async def resolve_seller_info(settings: Settings | None = None, use_mock: bool | None = None, vendor_id: str | None = None) -> dict:
    """출고지/반품지를 쿠팡 API로 직접 조회해, 사용자가 수동 입력하지 않아도 되는
    `seller_info` 딕셔너리를 자동으로 구성한다 (등록된 것 중 `usable=True`인 첫 번째 항목 사용).
    """
    settings = settings or get_settings()
    vendor_id = vendor_id or settings.coupang_vendor_id
    client = CoupangWingClient(settings=settings, use_mock=use_mock)

    outbound_places = await client.fetch_outbound_shipping_places(vendor_id)
    print(f"  [진단] 출고지 응답 원본: {outbound_places}", flush=True)
    outbound = next((p for p in outbound_places if p.get("usable")), None)
    if outbound is None:
        raise CoupangRegistrationError(
            "사용 가능한(usable) 출고지가 쿠팡 계정에 없습니다. WING 판매자센터에서 출고지를 먼저 등록해주세요."
        )

    # 반품지 조회 API(returnShippingCenters)가 WING 화면엔 "사용중"으로 실제 존재하는
    # 반품지를 빈 배열로 돌려주는 문제(캐시 지연으로 추정, 2026-09-21 실계정 확인됨)가
    # 있어, COUPANG_RETURN_CENTER_CODE를 .env에 채워두면 API 조회를 건너뛰고 그 값을
    # 바로 쓴다. 비워두면 기존처럼 API로 자동 조회한다.
    if settings.coupang_return_center_code:
        print("  [진단] 반품지: .env 수동 지정값 사용 (API 조회 건너뜀)", flush=True)
        return {
            "delivery_company_code": "CJGLS",
            "outbound_shipping_place_code": str(outbound.get("outboundShippingPlaceCode", "")),
            "return_center_code": settings.coupang_return_center_code,
            "return_charge_name": settings.coupang_return_charge_name,
            "company_contact_number": settings.coupang_return_contact_number,
            "return_zip_code": settings.coupang_return_zip_code,
            "return_address": settings.coupang_return_address,
            "return_address_detail": settings.coupang_return_address_detail,
        }

    return_centers = await client.fetch_return_shipping_centers(vendor_id)
    print(f"  [진단] 반품지 응답 원본: {return_centers}", flush=True)
    return_center = next((r for r in return_centers if r.get("usable")), None)
    if return_center is None:
        raise CoupangRegistrationError(
            "사용 가능한(usable) 반품지가 쿠팡 계정에 없습니다. WING 판매자센터에서 반품지를 먼저 등록해주세요 "
            "(또는 .env의 COUPANG_RETURN_CENTER_CODE 등을 채워서 수동으로 지정하세요)."
        )
    place_address = (return_center.get("placeAddresses") or [{}])[0]

    return {
        "delivery_company_code": "CJGLS",
        "outbound_shipping_place_code": str(outbound.get("outboundShippingPlaceCode", "")),
        "return_center_code": str(return_center.get("returnCenterCode", "")),
        "return_charge_name": return_center.get("shippingPlaceName", ""),
        "company_contact_number": place_address.get("companyContactNumber", ""),
        "return_zip_code": place_address.get("returnZipCode", ""),
        "return_address": place_address.get("returnAddress1", ""),
        "return_address_detail": place_address.get("returnAddress2", ""),
    }


async def predict_display_category_code(
    product_name: str, settings: Settings | None = None, use_mock: bool | None = None
) -> int:
    """상품명으로 쿠팡 전시카테고리 코드를 자동 추천받는다.

    사용자가 WING에서 수동으로 카테고리를 찾아 넣지 않아도 되도록,
    `product_pipeline.py`가 등록 직전에 이 함수를 호출해 `display_category_code`를
    자동으로 채운다 (사용자가 고급 옵션에서 직접 값을 지정하면 그 값이 우선한다).
    """
    settings = settings or get_settings()
    client = CoupangWingClient(settings=settings, use_mock=use_mock)
    result = await client.predict_category(product_name)
    category_id = result.get("predictedCategoryId")
    if not category_id:
        raise CoupangRegistrationError(f"카테고리 자동 추천 실패 (상품명={product_name!r}): {result}")
    return int(category_id)


def select_notice_category(metadata: dict) -> tuple[str, list[str]]:
    """카테고리 메타정보 응답에서 쓸 상품정보제공고시 카테고리 1개를 고른다.

    "신발"이 후보에 있으면 그걸 우선하고, 없으면 첫 번째 후보를 쓴다 — 한 카테고리
    안에서 항목을 섞어 쓰면 안 되므로(예: "신발" 항목 일부 + "기타재화" 항목 일부),
    반드시 후보 중 하나를 통째로 선택해야 한다.
    """
    categories = metadata.get("noticeCategories") or []
    if not categories:
        raise CoupangRegistrationError(f"카테고리 메타정보에 noticeCategories가 없습니다: {metadata}")

    chosen = next((c for c in categories if "신발" in c.get("noticeCategoryName", "")), categories[0])
    detail_names = [d.get("name", "") for d in chosen.get("noticeCategoryDetailNames") or [] if d.get("name")]
    if not detail_names:
        raise CoupangRegistrationError(f"선택된 고시카테고리에 항목이 없습니다: {chosen}")
    return chosen.get("noticeCategoryName", ""), detail_names


async def resolve_notice_info(
    display_category_code: int, settings: Settings | None = None, use_mock: bool | None = None
) -> tuple[str, list[str]]:
    """전시카테고리 코드로 그 카테고리가 실제로 요구하는 상품정보제공고시 항목을 조회한다.

    실패하면(권한 문제 등) 폴백으로 예시값(DEFAULT_NOTICE_CATEGORY/KEYS)을 쓴다 —
    카테고리 자동추천과 같은 이유로, 이 부가 기능 하나 때문에 등록 전체가 막히면 안 된다.
    호출부(product_pipeline.py)에서 이 폴백 여부를 로그로 남긴다.
    """
    settings = settings or get_settings()
    client = CoupangWingClient(settings=settings, use_mock=use_mock)
    metadata = await client.fetch_category_metadata(display_category_code)
    print(f"  [진단] 카테고리 고시정보 메타데이터 원본: {metadata}", flush=True)
    result = select_notice_category(metadata)
    print(f"  [진단] 선택된 고시카테고리/항목: {result}", flush=True)
    return result


def register_product_for_master_product(
    session: Session,
    product_id: int,
    display_category_code: int,
    selling_price: Decimal,
    size_stock: dict[str, dict],
    use_mock: bool | None = None,
    vendor_id: str | None = None,
    seller_info: dict | None = None,
    request_approval: bool = False,
) -> MarketListing:
    """master_products 1건을 쿠팡에 등록(또는 Mock 검증)하고 market_listings에 결과를 저장한다.

    `vendor_id`를 생략하면 설정(`COUPANG_VENDOR_ID`)값을 쓴다 — 테스트에서 실제 계정 없이
    임의의 벤더ID로 페이로드 생성을 검증할 때 override 용도로 쓴다.
    `seller_info`를 생략하면 출고지/반품지 코드를 쿠팡 API로 직접 조회해 자동으로 채운다
    (사용자가 WING 판매자센터에서 수동으로 값을 찾아 입력할 필요가 없다).
    `request_approval=False`(기본값)면 쿠팡에 임시저장만 되고 실제 판매 심사요청은 나가지
    않는다 — 실계정으로 처음 테스트할 때는 반드시 기본값(False)으로 두고, WING 판매자센터에서
    등록된 내용을 눈으로 확인한 뒤에만 True로 바꿔 승인요청을 보낸다.
    """
    settings = get_settings()
    vendor_id = vendor_id or settings.coupang_vendor_id

    product = session.get(MasterProduct, product_id)
    if product is None:
        raise ValueError(f"product_id={product_id} 인 master_products 행이 없습니다.")

    asset = session.query(GeneratedAsset).filter_by(product_id=product_id).first()
    if asset is None or not asset.ai_thumbnail_url or not asset.ai_detail_image_url:
        raise ValueError(
            f"product_id={product_id}의 AI 생성 이미지가 없습니다. "
            "asset_generation_tasks.generate_assets_for_product를 먼저 실행하세요."
        )

    if seller_info is None:
        seller_info = asyncio.run(resolve_seller_info(settings=settings, use_mock=use_mock, vendor_id=vendor_id))

    try:
        notice_category_name, notice_detail_keys = asyncio.run(
            resolve_notice_info(display_category_code, settings=settings, use_mock=use_mock)
        )
    except (CoupangRegistrationError, httpx.HTTPError) as exc:
        # 부가 기능(정확한 고시항목 자동조회) 하나 때문에 등록 전체가 막히면 안 된다 —
        # 실패하면 예시값(실API 미검증)으로 폴백하고 넘어간다.
        notice_category_name, notice_detail_keys = DEFAULT_NOTICE_CATEGORY, list(DEFAULT_NOTICE_DETAIL_KEYS)
        print(f"  카테고리 고시정보 자동조회 실패({exc}) — 기본값({notice_category_name})으로 등록합니다.")

    payload_input = CoupangProductInput(
        style_code=product.style_code,
        brand_name=product.brand_name,
        product_name=product.product_name,
        display_category_code=display_category_code,
        selling_price=selling_price,
        vendor_id=vendor_id,
        vendor_user_id=settings.coupang_vendor_user_id,
        thumbnail_image_url=asset.ai_thumbnail_url,
        detail_image_url=asset.ai_detail_image_url,
        size_stock=size_stock,
        specs=product.raw_specs_json or {},
        request_approval=request_approval,
        notice_category_name=notice_category_name,
        notice_detail_keys=tuple(notice_detail_keys),
    )
    payload = build_seller_product_payload(payload_input, seller_info)

    client = CoupangWingClient(settings=settings, use_mock=use_mock)
    response = asyncio.run(client.register_product(payload))

    if response.get("code") != "SUCCESS":
        raise CoupangRegistrationError(f"쿠팡 상품 등록 실패: {response}")

    listing = session.query(MarketListing).filter_by(product_id=product_id, market_type=MarketType.COUPANG).first()
    if listing is None:
        listing = MarketListing(product_id=product_id, market_type=MarketType.COUPANG)
        session.add(listing)

    listing.market_product_id = str(response["data"])
    listing.selling_price = selling_price
    # request_approval=False로 등록하면 쿠팡 쪽엔 임시저장 상태로만 들어가므로,
    # 우리 DB 상태도 실제 판매중(ACTIVE)이 아니라 임시저장(DRAFT)으로 맞춰야 한다 —
    # 아니면 아직 심사요청도 안 나간 상품을 대시보드에 "판매중"으로 잘못 보여주게 된다.
    listing.status = ListingStatus.ACTIVE if request_approval else ListingStatus.DRAFT
    listing.registered_at = datetime.now(UTC)
    session.commit()

    return listing
