# app/models/support_ticket.py
"""Help & Support requests (frontend ask 14)."""
from datetime import datetime
from typing import Optional

from beanie import Document, Link
from pydantic import BaseModel, Field

from app.core.time import utc_now
from app.models.user import User

SUPPORT_TICKET_STATUSES = ("open", "in_progress", "resolved", "closed")


class SupportTicket(Document):
    ticket_number: str
    user: Link[User]
    subject: str
    message: str
    booking_id: Optional[str] = None
    status: str = "open"  # one of SUPPORT_TICKET_STATUSES
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "support_tickets"
        indexes = ["user", "ticket_number"]


class SupportTicketCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=150)
    message: str = Field(min_length=1, max_length=5000)
    booking_id: Optional[str] = None


class SupportTicketResponse(BaseModel):
    id: str
    ticket_number: str
    subject: str
    message: str
    booking_id: Optional[str] = None
    status: str
    created_at: datetime
