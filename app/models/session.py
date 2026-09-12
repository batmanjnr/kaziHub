# app/models/session.py
"""Refresh-token sessions (spec §3): stored hashed, never in plaintext.
`family_id` groups tokens issued from the same original login so rotation
reuse-detection can revoke the whole family if a used token is replayed."""
from datetime import datetime
from beanie import Document, Link

from app.models.user import User


class UserSession(Document):
    user: Link[User]
    refresh_token_hash: str
    family_id: str
    is_revoked: bool = False
    expires_at: datetime
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "user_sessions"
        indexes = ["refresh_token_hash", "family_id", "user"]
