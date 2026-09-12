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
    state: str
    role: str
    hashed_password: str
    otp_code: str
    otp_expires_at: datetime
    otp_attempts: int = 0
    created_at: datetime

    class Settings:
        name = "pending_users"