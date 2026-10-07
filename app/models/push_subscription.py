# app/models/push_subscription.py
"""Browser/PWA Web Push subscriptions (frontend ask 44): one row per device
that allowed notifications."""
from datetime import datetime
from typing import Optional

from beanie import Document, Indexed, Link
from pydantic import BaseModel, Field

from app.core.time import utc_now
from app.models.user import User


class PushSubscription(Document):
    user: Link[User]
    endpoint: Indexed(str, unique=True)
    p256dh: str
    auth: str
    user_agent: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "push_subscriptions"
        indexes = ["user"]


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionCreate(BaseModel):
    """Exactly what the browser's `PushSubscription.toJSON()` returns."""
    endpoint: str = Field(min_length=10, max_length=1000, pattern=r"^https://")
    keys: PushSubscriptionKeys
    expirationTime: Optional[float] = None


class PushSubscriptionDelete(BaseModel):
    endpoint: str


class PushPublicKeyResponse(BaseModel):
    enabled: bool = Field(description="False until the server has VAPID keys configured.")
    public_key: Optional[str] = Field(
        default=None, description="Pass as `applicationServerKey` to `pushManager.subscribe()`."
    )
