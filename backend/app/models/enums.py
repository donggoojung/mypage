import enum


class SourcePlatform(str, enum.Enum):
    """PRD 2단계: 소싱처 — ABC마트, 무신사, 폴더, 브랜드 공식 자사몰 등."""

    ABC_MART = "abc_mart"
    MUSINSA = "musinsa"
    FOLDER = "folder"
    BRAND_OFFICIAL = "brand_official"
    OTHER = "other"


class MarketType(str, enum.Enum):
    """PRD 3단계: 등록 대상 오픈마켓."""

    NAVER_SMARTSTORE = "naver_smartstore"
    COUPANG = "coupang"
    GMARKET = "gmarket"
    TOSS = "toss"


class GenerationStatus(str, enum.Enum):
    """PRD 3.2/3.3: AI 이미지·상세페이지 생성 파이프라인 상태."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ListingStatus(str, enum.Enum):
    """PRD 9.2: 마켓 상품 판매 상태 (품절 시 즉시 판매중지 전환)."""

    DRAFT = "draft"
    ACTIVE = "active"
    SOLD_OUT = "sold_out"
    SUSPENDED = "suspended"


class OrderStatus(str, enum.Enum):
    """PRD 5단계 서술 기준 고객 주문 파이프라인 상태."""

    RECEIVED = "received"
    SOURCING_IN_PROGRESS = "sourcing_in_progress"
    # 30분 재고 동기화 주기 사이의 순간 품절/발주 실패 등 사람이 확인해야 하는 상황 —
    # 자동 재시도하지 않고 여기서 멈춰서 관리자 텔레그램 알림과 함께 대기한다.
    HOLD = "hold"
    ORDER_PURCHASED = "order_purchased"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class InspectionStatus(str, enum.Enum):
    """PRD 7단계: 반품 거점 창고 검수 및 소싱처 반품/분쟁 상태."""

    RECEIVED_AT_WAREHOUSE = "received_at_warehouse"
    INSPECTION_PASSED = "inspection_passed"
    INSPECTION_FAILED = "inspection_failed"
    SOURCE_RETURN_REQUESTED = "source_return_requested"
    SOURCE_RETURN_COMPLETED = "source_return_completed"
    DISPUTE_INVESTIGATING = "dispute_investigating"
    REFUND_COMPLETED = "refund_completed"
