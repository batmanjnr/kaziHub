# app/models/notification.py
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from pydantic import BaseModel

from app.models.booking import Booking
from app.models.user import User


class Notification(Document):
    user: Link[User]
    type: str
    title: str
    message: str
    booking: Optional[Link[Booking]] = None
    is_read: bool = False
    channel: str = "in_app"  # "in_app" | "sms" | "whatsapp" | "email"
    delivery_status: str = "pending"  # "pending" | "sent" | "failed"
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "notifications"
        indexes = ["user"]


class NotificationResponse(BaseModel):
    id: str
    type: str
    title: str
    message: str
    booking_id: Optional[str] = None
    is_read: bool
    channel: str
    created_at: datetime
