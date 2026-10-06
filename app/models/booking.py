import re
from datetime import date, datetime
from enum import Enum
from typing import Annotated, List, Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import AfterValidator, BaseModel, Field
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
    attachments: List[str] = Field(default_factory=list)

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
    completion_photos: List[str] = Field(default_factory=list)
    completion_video_url: Optional[str] = None
    # 4-day auto-release deadline, set when completion is submitted; the
    # auto-release cron (Phase 10) queries on this field.
    auto_completion_deadline: Optional[datetime] = None

    cancellation_reason: Optional[str] = None
    cancelled_by: Optional[Link[User]] = None
    cancelled_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "bookings"
        indexes = ["client", "artisan", "reference_code"]


# --- Schemas ---

_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


def _check_window(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    m = _WINDOW_RE.match(value)
    if not m or value[:5] >= value[6:]:
        raise ValueError("scheduled_window must look like '09:00-11:00' with the start before the end.")
    return value


def _check_not_past(value: Optional[date]) -> Optional[date]:
    if value is not None and value < date.today():
        raise ValueError("scheduled_date can't be in the past.")
    return value


ScheduledWindow = Annotated[str, AfterValidator(_check_window)]
ScheduledDate = Annotated[date, AfterValidator(_check_not_past)]


class FixedBookingCreate(BaseModel):
    artisan_id: str
    # Ask 24: the price and title always come from this service on the
    # server. service_title/amount are accepted for older clients but
    # ignored.
    service_id: str
    service_title: Optional[str] = Field(default=None, description="Ignored; taken from the service.")
    amount: Optional[float] = Field(default=None, description="Ignored; taken from the service.")
    description: str = Field(min_length=1, max_length=2000)
    address: str = Field(min_length=1, max_length=300)
    landmark_hint: Optional[str] = Field(default=None, max_length=200)
    attachments: List[str] = Field(default_factory=list, max_length=10)
    scheduled_date: Optional[ScheduledDate] = None
    scheduled_window: Optional[ScheduledWindow] = Field(default=None, description="e.g. '09:00-11:00'")


class CustomQuoteRequestCreate(BaseModel):
    artisan_id: str
    service_title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    address: str = Field(min_length=1, max_length=300)
    landmark_hint: Optional[str] = Field(default=None, max_length=200)
    attachments: List[str] = Field(
        default_factory=list, max_length=10, description="Photo URLs from POST /bookings/upload."
    )
    scheduled_date: Optional[ScheduledDate] = None
    scheduled_window: Optional[ScheduledWindow] = Field(default=None, description="e.g. '09:00-11:00'")


class SubmitCompletionSchema(BaseModel):
    completion_description: Optional[str] = Field(default=None, max_length=2000)
    completion_photos: List[str] = Field(
        default_factory=list, max_length=10, description="Photo URLs from POST /bookings/upload."
    )


class GigPurchaseCreate(BaseModel):
    artisan_id: str
    gig_id: str
    item_title: str
    # No client-supplied amount: the booking's price is always taken from
    # the gig's own listed price server-side (spec/security fix — a client
    # could otherwise buy any gig for an arbitrary amount).
    delivery_address: str = Field(min_length=1, max_length=300)
    landmark_hint: Optional[str] = Field(default=None, max_length=200)


class SendQuoteSchema(BaseModel):
    amount: float = Field(gt=0)
    breakdown: Optional[str] = Field(None, description="e.g. Materials: ₦20k, Labor: ₦15k")


class BookingResponse(BaseModel):
    id: str
    client_id: str
    artisan_id: str
    gig_id: Optional[str] = None
    booking_type: BookingType
    title: str
    description: Optional[str] = None
    attachments: List[str] = Field(default_factory=list)
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
    scheduled_window: Optional[str] = None
    completion_description: Optional[str] = None
    completion_photos: List[str] = Field(default_factory=list)
    auto_completion_deadline: Optional[datetime] = None
    lock_version: int = 0
    created_at: datetime
    # The other party's details (ask 29). Phones follow each person's
    # phone_visibility setting and are null until it allows sharing.
    client_name: Optional[str] = None
    client_avatar: Optional[str] = None
    client_phone: Optional[str] = None
    artisan_name: Optional[str] = None
    artisan_avatar: Optional[str] = None
    artisan_phone: Optional[str] = None
    artisan_profile_id: Optional[str] = None
    artisan_category: Optional[str] = None


class BookingStatusHistoryEntry(BaseModel):
    from_status: Optional[str] = None
    to_status: str
    changed_by: Optional[str] = None
    reason: Optional[str] = None
    created_at: datetime


class BookingDetailResponse(BookingResponse):
    timeline: List[BookingStatusHistoryEntry] = Field(default_factory=list)
