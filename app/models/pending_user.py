from datetime import datetime
from beanie import Document
from pydantic import EmailStr


class PendingUser(Document):
    first_name: str
    last_name: str
    email: EmailStr
    phone_number: str
    nin: str
    state: str
    role: str
    hashed_password: str
    otp_code: str
    otp_expires_at: datetime
    created_at: datetime

    class Settings:
        name = "pending_users"