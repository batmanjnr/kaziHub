from datetime import datetime
from typing import List, Optional
from beanie import Document, Indexed
from pydantic import BaseModel, EmailStr
from pymongo import IndexModel


class User(Document):
    first_name: str
    last_name: str
    email: Indexed(EmailStr, unique=True)
    phone_number: str
    state: str
    # Kept for backward compatibility with existing auth/booking logic during
    # the phased migration; capability is being moved to `roles` (see
    # app/models/user_role.py) and will replace this field once auth is
    # rewritten (Phase 2).
    role: str  # "client" or "artisan"
    roles: List[str] = []
    is_admin: bool = False
    is_frozen: bool = False
    two_factor_enabled: bool = False
    two_factor_secret_encrypted: Optional[bytes] = None
    # Bumped whenever an admin suspends/reactivates the account or the user
    # revokes sessions; access tokens carry the version they were issued at
    # and are rejected if it no longer matches.
    token_version: int = 0
    nin_encrypted: Optional[bytes] = None
    # Deterministic HMAC of the NIN — enforces one-account-per-NIN via the
    # sparse unique index below, without the NIN itself ever being
    # queryable or reversible from this value. Absent when no NIN was
    # provided; `sparse=True` is critical here — a plain unique index on an
    # optional field lets only one `null` document ever exist (this exact
    # bug hit the old plaintext `nin` unique index and silently broke
    # registration for every user after the first).
    nin_hash: Optional[str] = None
    deleted_at: Optional[datetime] = None
    anonymized_at: Optional[datetime] = None
    hashed_password: str
    is_active: bool = True
    is_email_verified: bool = False
    is_phone_verified: bool = False
    otp_code: Optional[str] = None
    otp_expires_at: Optional[datetime] = None
    reset_otp_code: Optional[str] = None
    reset_otp_expires_at: Optional[datetime] = None
    reset_otp_attempts: int = 0
    profile_picture: Optional[str] = None
    theme: str = "system"  # "light", "dark", or "system"
    preferred_language: str = "en"  # "en", "es", "fr", etc.
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "users"
        indexes = [
            # FIX (live-DB testing): `sparse=True` alone isn't enough here —
            # Beanie writes every Optional field explicitly, so a user
            # without a NIN still gets a literal `nin_hash: null` in the
            # stored document. MongoDB's sparse index only excludes a
            # field that's genuinely *absent*, not one present with value
            # null — so a plain sparse index still collided every no-NIN
            # user against each other, the exact same bug this index was
            # built to fix in the first place. A partial index with an
            # explicit type filter is what actually only indexes real
            # string hashes.
            IndexModel(
                [("nin_hash", 1)],
                unique=True,
                partialFilterExpression={"nin_hash": {"$type": "string"}},
            ),
        ]

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class UserCreate(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    password: str
    phone_number: str
    nin: Optional[str] = None
    state: str
    role: str


class UserUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone_number: Optional[str] = None
    state: Optional[str] = None
    nin: Optional[str] = None
    profile_picture: Optional[str] = None
    theme: Optional[str] = None
    preferred_language: Optional[str] = None


class VerifyEmailSchema(BaseModel):
    email: EmailStr
    otp: str


class ResendOTPSchema(BaseModel):
    email: EmailStr


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    user_id: Optional[str] = None


class UserResponse(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: EmailStr
    phone_number: str
    nin_masked: Optional[str] = None
    state: str
    role: str
    roles: List[str] = []
    is_admin: bool = False
    is_active: bool
    is_email_verified: bool
    profile_picture: Optional[str] = None
    theme: str = "system"
    preferred_language: str = "en"
    created_at: datetime
