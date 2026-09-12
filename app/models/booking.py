from datetime import date, datetime
from enum import Enum
from typing import List, Optional
from beanie import Document, Link
from pydantic import BaseModel, Field
from app.models.user import User


class BookingType(str, Enum):
    FIXED_SERVICE = "fixed_service"      # Fixed rate service
    CUSTOM_QUOTE = "custom_quote"        # Requires artisan quote/estimate
    GIG_PURCHASE = "gig_purchase"        # Direct purchase of a listed item/gig


class BookingStatus(str, Enum):
    QUOTE_REQUESTED = "quote_requested"  # Client requested quote
    QUOTE_SENT = "quote_sent"            # Artisan provided custom estimate
    PENDING = "pending"                  # Fixed service / Gig purchase awaiting artisan acceptance
    ACCEPTED = "accepted"                # Quote accepted or booking accepted
    ESCROW_FUNDED = "escrow_funded"      # Payment locked in escrow
    IN_PROGRESS = "in_progress"          # Service underway / item being delivered
    COMPLETED_BY_ARTISAN = "completed_by_artisan"
    PAID_OUT = "paid_out"                # Funds released to artisan
    CANCELLED = "cancelled"
    DISPUTED = "disputed"


class EscrowStatus(str, Enum):
    UNFUNDED = "unfunded"
    HELD_IN_ESCROW = "held_in_escrow"
    RELEASED_TO_ARTISAN = "released_to_artisan"
    REFUNDED_TO_CLIENT = "refunded_to_client"
    PARTIALLY_REFUNDED = "partially_refunded"


class Booking(Document):
    client: Link[User]
    artisan: Link[User]
    gig_id: Optional[str] = None
    booking_type: BookingType = BookingType.FIXED_SERVICE

    # Spec's human-readable, unique lookup code for the booking, distinct from
    # the Mongo _id (used in receipts, support tickets, Paystack metadata).
    reference_code: Optional[str] = None

    title: str
    description: Optional[str] = None
    attachments: List[str] = []

    amount: float = 0.0
    quote_breakdown: Optional[str] = None

    address: Optional[str] = None
    landmark_hint: Optional[str] = None

    status: BookingStatus = BookingStatus.PENDING
    payment_reference: Optional[str] = None

    # --- Escrow ledger fields (§4.7) — populated by the Phase 4/9 rewrite of
    # the booking + wallet endpoints; present now so the schema matches spec
    # and downstream phases don't need another migration. ---
    escrow_status: EscrowStatus = EscrowStatus.UNFUNDED
    escrow_amount: float = 0.0
    platform_fee: float = 0.0
    gateway_fee: float = 0.0
    artisan_earnings: float = 0.0
    platform_commission_rate: float = Field(default=0.10, ge=0, le=1)
    escrow_funded_at: Optional[datetime] = None
    escrow_released_at: Optional[datetime] = None
    # Optimistic-locking version, checked-and-incremented on every write that
    # touches escrow_status (Mongo equivalent of SELECT ... FOR UPDATE +
    # serializable transaction — see §6). A write whose filter includes a
    # stale lock_version simply matches zero documents and must retry.
    lock_version: int = 0

    scheduled_date: Optional[date] = None
    scheduled_time_slot: Optional[str] = None

    completion_submitted_at: Optional[datetime] = None
    completion_description: Optional[str] = None
    completion_photos: List[str] = []
    completion_video_url: Optional[str] = None
    # 4-day auto-release deadline, set when completion is submitted; the
    # auto-release cron (Phase 10) queries on this field.
    auto_completion_deadline: Optional[datetime] = None

    cancellation_reason: Optional[str] = None
    cancelled_by: Optional[Link[User]] = None
    cancelled_at: Optional[datetime] = None

    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "bookings"
        indexes = ["client", "artisan", "reference_code"]


# --- Schemas ---

class FixedBookingCreate(BaseModel):
    artisan_id: str
    service_title: str
    amount: float
    description: str
    address: str
    landmark_hint: Optional[str] = None
    attachments: List[str] = []


class CustomQuoteRequestCreate(BaseModel):
    artisan_id: str
    service_title: str
    description: str
    address: str
    landmark_hint: Optional[str] = None
    attachments: List[str] = []


class GigPurchaseCreate(BaseModel):
    artisan_id: str
    gig_id: str
    item_title: str
    # No client-supplied amount: the booking's price is always taken from
    # the gig's own listed price server-side (spec/security fix — a client
    # could otherwise buy any gig for an arbitrary amount).
    delivery_address: str
    landmark_hint: Optional[str] = None


class SendQuoteSchema(BaseModel):
    amount: float
    breakdown: Optional[str] = Field(None, description="e.g. Materials: ₦20k, Labor: ₦15k")


class BookingResponse(BaseModel):
    id: str
    client_id: str
    artisan_id: str
    gig_id: Optional[str] = None
    booking_type: BookingType
    title: str
    description: Optional[str] = None
    attachments: List[str] = []
    amount: float
    quote_breakdown: Optional[str] = None
    address: Optional[str] = None
    landmark_hint: Optional[str] = None
    status: BookingStatus
    escrow_status: EscrowStatus = EscrowStatus.UNFUNDED
    payment_reference: Optional[str] = None
    reference_code: Optional[str] = None
    escrow_amount: float = 0.0
    platform_fee: float = 0.0
    gateway_fee: float = 0.0
    artisan_earnings: float = 0.0
    platform_commission_rate: float = 0.10
    scheduled_date: Optional[date] = None
    completion_description: Optional[str] = None
    completion_photos: List[str] = []
    auto_completion_deadline: Optional[datetime] = None
    lock_version: int = 0
    created_at: datetime


class BookingStatusHistoryEntry(BaseModel):
    from_status: Optional[str] = None
    to_status: str
    changed_by: Optional[str] = None
    reason: Optional[str] = None
    created_at: datetime


class BookingDetailResponse(BookingResponse):
    timeline: List[BookingStatusHistoryEntry] = []
