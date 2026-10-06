from datetime import datetime
from typing import Optional
from beanie import Document
from pydantic import EmailStr


class PendingUser(Document):
    first_name: str
    last_name: str
    email: EmailStr
    phone_number: str
    # FIX: encrypted at rest — this is a government ID number, staged or not.
    nin_encrypted: Optional[bytes] = None
    # Carried through to the real User document on verification (see
    # app.core.nin_hash) — computed here, while the plaintext NIN is still
    # in hand, since nin_encrypted alone can't be turned back into it.
    nin_hash: Optional[str] = None
    state: str
    role: str
    hashed_password: str
    otp_code: str
    otp_expires_at: datetime
    otp_attempts: int = 0
    terms_version: Optional[str] = None
    terms_accepted_at: Optional[datetime] = None
    created_at: datetime

    class Settings:
        name = "pending_users"