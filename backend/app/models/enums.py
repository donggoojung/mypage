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
    # 반자동 승인(PRD 5.2 안전장치 강화): RPA_CONFIRM_FINAL_PAYMENT=false(기본값)일 때, RPA가
    # 배송지 입력까지 마치고 실제 결제 버튼 직전에서 멈추면 에러(HOLD)가 아니라 이 상태로
    # 전환된다 — 대시보드에서 관리자가 가격/배송지를 눈으로 확인하고 [결제 승인]을 눌러야
    # order_processor.approve_and_complete_purchase()가 실제 결제를 이어서 완료한다.
    PENDING_PAYMENT_APPROVAL = "pending_payment_approval"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    # PRD 7장 역물류: 소싱처(ABC마트)에 이미 매입 비용이 나간 뒤(ORDER_PURCHASED/SHIPPED)에
    # 쿠팡 취소가 감지된 경우 — 배송비/매입비 손실 위험이 있어 CANCELLED로 바로 넘기지
    # 않고 관리자 확인이 필요한 상태로 멈춘다(HOLD와 유사한 "사람 확인 필요" 상태).
    CANCEL_REQUESTED = "cancel_requested"
    # 고객이 배송완료 후 쿠팡에서 반품을 접수한 경우 — return_requests에 상세 내역이 쌓인다.
    RETURN_REQUESTED = "return_requested"


class InspectionStatus(str, enum.Enum):
    """PRD 7단계: 반품 거점 창고 검수 및 소싱처 반품/분쟁 상태."""

    RECEIVED_AT_WAREHOUSE = "received_at_warehouse"
    INSPECTION_PASSED = "inspection_passed"
    INSPECTION_FAILED = "inspection_failed"
    SOURCE_RETURN_REQUESTED = "source_return_requested"
    SOURCE_RETURN_COMPLETED = "source_return_completed"
    DISPUTE_INVESTIGATING = "dispute_investigating"
    REFUND_COMPLETED = "refund_completed"
