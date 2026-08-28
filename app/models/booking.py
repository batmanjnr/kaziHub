from datetime import datetime
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


class Booking(Document):
    client: Link[User]
    artisan: Link[User]
    gig_id: Optional[str] = None
    booking_type: BookingType = BookingType.FIXED_SERVICE

    title: str
    description: Optional[str] = None
    attachments: List[str] = []
    
    amount: float = 0.0
    quote_breakdown: Optional[str] = None
    
    address: Optional[str] = None
    landmark_hint: Optional[str] = None

    status: BookingStatus = BookingStatus.PENDING
    payment_reference: Optional[str] = None

    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "bookings"


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
    amount: float
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
    payment_reference: Optional[str] = None
    created_at: datetime