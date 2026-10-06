# app/models/session.py
"""Refresh-token sessions (spec §3): stored hashed, never in plaintext.
`family_id` groups tokens issued from the same original login so rotation
reuse-detection can revoke the whole family if a used token is replayed.
It's also the stable session id (`sid` claim in access tokens, `id` in
GET /auth/sessions): refreshing creates a new row in the same family, so
the device keeps one id for its whole login."""
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import Field

from app.models.user import User


class UserSession(Document):
    user: Link[User]
    refresh_token_hash: str
    family_id: str
    is_revoked: bool = False
    # Why this row stopped being usable: "rotated" (spent by a refresh — a
    # replay of it means theft), or "signed_out" (logout, revoke, password
    # change). Lets /auth/refresh tell a stolen token from a signed-out
    # device (ask 19). None on rows from before this field existed.
    revoked_reason: Optional[str] = None
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    # Populated at login/refresh from the request; drive the "Active
    # Devices & Sessions" list so a user can see and revoke individual
    # sessions instead of only the blanket revoke-all.
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    last_used_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "user_sessions"
        indexes = ["refresh_token_hash", "family_id", "user"]
