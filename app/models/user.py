from datetime import datetime
from typing import List, Optional
from beanie import Document, Indexed
from pydantic import BaseModel, EmailStr


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
