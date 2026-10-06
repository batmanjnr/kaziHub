# app/models/dispute.py
from datetime import datetime
from typing import List, Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field

from app.models.booking import Booking
from app.models.user import User

DISPUTE_STATUSES = (
    "under_review",
    "artisan_responding",
    "arbitration",
    "resolved_client_refund",
    "resolved_artisan_paid",
    "resolved_split",
)


class Dispute(Document):
    ticket_id: str
    booking: Link[Booking]
    client: Link[User]
    artisan: Link[User]
    reason: str
    details: str
    evidence_photos: List[str] = Field(default_factory=list)
    status: str = "under_review"  # one of DISPUTE_STATUSES
    resolution_notes: Optional[str] = None
    resolved_by: Optional[Link[User]] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "disputes"
        indexes = ["ticket_id", "booking"]


class DisputeCreate(BaseModel):
    reason: str
    details: str
    evidence_photos: List[str] = Field(default_factory=list)


class DisputeResolve(BaseModel):
    resolution: str  # "client_refund" | "artisan_paid" | "split"
    split_ratio: Optional[float] = None
    resolution_notes: Optional[str] = None


class DisputeResponse(BaseModel):
    id: str
    ticket_id: str
    booking_id: str
    client_id: str
    artisan_id: str
    reason: str
    details: str
    evidence_photos: List[str]
    status: str
    resolution_notes: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime
