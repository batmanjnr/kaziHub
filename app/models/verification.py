# app/models/verification.py
from datetime import datetime
from enum import Enum
from typing import Optional
from beanie import Document, Link
from pydantic import BaseModel

from app.models.user import User


class VerificationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Verification(Document):
    user: Link[User]
    nin: str
    id_type: str  # e.g., "NIN", "Voters Card", "Drivers License", "Passport"
    id_number: str
    id_card_image: str  # Image URL or file path
    selfie_image: str   # Image URL or file path
    status: VerificationStatus = VerificationStatus.PENDING
    rejection_reason: Optional[str] = None
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "verifications"


class VerificationSubmit(BaseModel):
    nin: str
    id_type: str
    id_number: str
    id_card_image: str
    selfie_image: str


class VerificationReview(BaseModel):
    status: VerificationStatus
    rejection_reason: Optional[str] = None


class VerificationResponse(BaseModel):
    id: str
    user_id: str
    nin: str
    id_type: str
    id_number: str
    id_card_image: str
    selfie_image: str
    status: VerificationStatus
    rejection_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime