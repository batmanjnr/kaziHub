# app/models/booking_status_history.py
"""Timeline entries for a booking. Written in the same transaction as every
status change (including cron-driven ones) so GET /bookings/:id can
reconstruct a real audit trail instead of only exposing current status.
"""
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field

from app.models.booking import Booking
from app.models.user import User


class BookingStatusHistory(Document):
    booking: Link[Booking]
    from_status: Optional[str] = None
    to_status: str
    changed_by: Optional[Link[User]] = None
    reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "booking_status_history"
        indexes = ["booking"]


class BookingStatusHistoryResponse(BaseModel):
    from_status: Optional[str] = None
    to_status: str
    changed_by: Optional[str] = None
    reason: Optional[str] = None
    created_at: datetime
