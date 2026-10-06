# app/models/notification.py
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field

from app.models.booking import Booking
from app.models.user import User


# Every value `Notification.type` can take (ask 31).
NOTIFICATION_TYPES = {
    "booking_requested": "A client booked you, requested a quote, or bought a gig (artisan).",
    "quote_sent": "The artisan sent a quote (client).",
    "quote_accepted": "The client accepted your quote (artisan).",
    "booking_accepted": "The artisan accepted your booking (client).",
    "booking_declined": "The artisan declined your booking (client).",
    "escrow_funded": "The client paid into escrow; you can start (artisan).",
    "job_started": "The artisan started the job (client).",
    "completion_submitted": "The artisan marked the job done; confirm before the auto-release deadline (client).",
    "payment_released": "Escrow was released and your payout sent (artisan).",
    "escrow_auto_released": "The job was auto-confirmed after 4 days and payment released (client).",
    "booking_cancelled": "The other party cancelled the booking.",
    "booking_disputed": "The other party opened a dispute.",
    "dispute_resolved": "An admin resolved the dispute (both parties).",
    "new_message": "A chat message arrived while you weren't connected to that chat live.",
    "new_review": "A client reviewed you (artisan).",
    "verification_review": "Your ID verification was approved or rejected.",
    "payout_issue": "A payout failed or was reversed (artisan).",
    "support_ticket_update": "Your support request was received or updated.",
}


class Notification(Document):
    user: Link[User]
    type: str
    title: str
    message: str
    booking: Optional[Link[Booking]] = None
    is_read: bool = False
    channel: str = "in_app"  # "in_app" | "sms" | "whatsapp" | "email"
    delivery_status: str = "pending"  # "pending" | "sent" | "failed"
    emailed_in_summary: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "notifications"
        indexes = ["user"]


class NotificationResponse(BaseModel):
    id: str
    type: str = Field(description="One of: " + ", ".join(NOTIFICATION_TYPES))
    title: str
    message: str
    booking_id: Optional[str] = None
    is_read: bool
    channel: str
    created_at: datetime


class NotificationPreferences(BaseModel):
    push_enabled: bool = True
    email_summaries: bool = True


class NotificationPreferencesUpdate(BaseModel):
    push_enabled: Optional[bool] = None
    email_summaries: Optional[bool] = None
