# app/models/verification.py
from datetime import datetime
from enum import Enum
from typing import Optional
from beanie import Document, Link
from pydantic import BaseModel

from app.models.user import User

DOCUMENT_TYPES = ("nin", "drivers_license", "voters_card", "passport")


class VerificationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Verification(Document):
    # FIX: no UNIQUE(user_id) — a rejected artisan can resubmit without
    # destroying rejection history. The current/authoritative submission is
    # the latest row by created_at.
    user: Link[User]
    document_type: str  # one of DOCUMENT_TYPES
    # FIX: encrypted at rest (NDPR §12) — this is a government ID number.
    document_number_encrypted: bytes
    # FIX (spec §9): KYC files live in a private Cloudinary bucket, never a
    # publicly reachable URL — these are Cloudinary public_ids, resolved to
    # a 15-minute signed URL only when building an API response.
    document_image_public_id: str
    document_image_format: str = "jpg"
    liveness_selfie_public_id: str
    liveness_selfie_format: str = "jpg"
    status: VerificationStatus = VerificationStatus.PENDING
    rejection_reason: Optional[str] = None
    reviewed_by: Optional[Link[User]] = None
    reviewed_at: Optional[datetime] = None
    # Explicit consent capture for biometric processing — NDPR requires
    # documented consent for biometric data specifically.
    biometric_consent_given_at: datetime
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "verifications"
        indexes = ["user"]


class VerificationSubmit(BaseModel):
    document_type: str
    document_number: str  # plaintext in transit only; encrypted before storage
    # From POST /verification/upload's response — a Cloudinary public_id,
    # not a URL (spec §9: private bucket).
    document_image_public_id: str
    document_image_format: str = "jpg"
    liveness_selfie_public_id: str
    liveness_selfie_format: str = "jpg"
    biometric_consent: bool


class VerificationReview(BaseModel):
    status: VerificationStatus
    rejection_reason: Optional[str] = None


class VerificationResponse(BaseModel):
    id: str
    user_id: str
    document_type: str
    # Masked, never the decrypted value — see app.core.encryption.mask_tail.
    document_number_masked: str
    document_image_url: str
    liveness_selfie_url: str
    status: VerificationStatus
    rejection_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
