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

# --- TODO: 실API 검증이 필요한 상품정보제공고시(신발 카테고리) 기본 항목 ---
DEFAULT_NOTICE_CATEGORY = "신발"
DEFAULT_NOTICE_DETAIL_KEYS = ("소재", "색상", "치수", "제조자(수입자)", "제조국", "세탁방법 및 취급시 주의사항")


class CoupangRegistrationError(RuntimeError):
    """쿠팡 상품 등록 API 호출/응답 처리 중 발생한 오류."""


def _generate_hmac_signature(method: str, path: str, secret_key: str, query: str = "") -> tuple[str, str]:
    """쿠팡 WING 오픈API 표준 HMAC-SHA256 서명을 생성한다.

    Returns: (signed_date, signature_hex)
    """
    now = datetime.now(UTC)
    signed_date = now.strftime("%y%m%d") + "T" + now.strftime("%H%M%S") + "Z"
    message = signed_date + method.upper() + path + query
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
    thumbnail_image_url: str
    detail_image_url: str
    size_stock: dict[str, dict]  # {"250": {"stock": 5, "is_sold_out": False}, ...}
    specs: dict[str, str] = field(default_factory=dict)  # {"소재": "...", "제조국": "...", ...}
    original_price: Decimal | None = None  # None이면 selling_price와 동일하게 처리(할인 없음)
    manufacturer: str = ""


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
        {"noticeCategoryName": DEFAULT_NOTICE_CATEGORY, "noticeCategoryDetailName": key, "content": data.specs.get(key, "상품 상세 참조")}
        for key in DEFAULT_NOTICE_DETAIL_KEYS
    ]
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
                "maximumBuyForPersonPeriod": 0,
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
        "deliveryMethod": "SEQUENCE",
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
        "requested": True,
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


def register_product_for_master_product(
    session: Session,
    product_id: int,
    display_category_code: int,
    selling_price: Decimal,
    size_stock: dict[str, dict],
    use_mock: bool | None = None,
    vendor_id: str | None = None,
) -> MarketListing:
    """master_products 1건을 쿠팡에 등록(또는 Mock 검증)하고 market_listings에 결과를 저장한다.

    `vendor_id`를 생략하면 설정(`COUPANG_VENDOR_ID`)값을 쓴다 — 테스트에서 실제 계정 없이
    임의의 벤더ID로 페이로드 생성을 검증할 때 override 용도로 쓴다.
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

    seller_info = {
        "delivery_company_code": "CJGLS",
        "return_center_code": settings.coupang_return_center_code,
        "return_charge_name": settings.coupang_return_charge_name,
        "company_contact_number": settings.coupang_company_contact_number,
        "return_zip_code": settings.coupang_return_zip_code,
        "return_address": settings.coupang_return_address,
        "return_address_detail": settings.coupang_return_address_detail,
        "outbound_shipping_place_code": settings.coupang_outbound_shipping_place_code,
    }

    payload_input = CoupangProductInput(
        style_code=product.style_code,
        brand_name=product.brand_name,
        product_name=product.product_name,
        display_category_code=display_category_code,
        selling_price=selling_price,
        vendor_id=vendor_id,
        thumbnail_image_url=asset.ai_thumbnail_url,
        detail_image_url=asset.ai_detail_image_url,
        size_stock=size_stock,
        specs=product.raw_specs_json or {},
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
    listing.status = ListingStatus.ACTIVE
    listing.registered_at = datetime.now(UTC)
    session.commit()

    return listing
