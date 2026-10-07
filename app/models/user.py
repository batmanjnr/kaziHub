from datetime import datetime
from typing import List, Optional
from beanie import Document, Indexed
from app.core.time import utc_now
from app.core.validators import Language, Name, Password, Phone, PhoneVisibility, State, Theme
from pydantic import BaseModel, EmailStr, Field
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
    roles: List[str] = Field(default_factory=list)
    is_admin: bool = False
    is_frozen: bool = False
    # Self-service "freeze account" (distinct from `is_frozen`, which is
    # the admin-suspend flag and blocks login entirely). A paused user can
    # still log in — that's how they unfreeze themselves — but their
    # artisan profile drops out of search and can't receive new bookings.
    is_paused: bool = False
    pending_email: Optional[str] = None
    email_change_otp: Optional[str] = None
    email_change_otp_expires_at: Optional[datetime] = None
    email_change_otp_attempts: int = 0
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
    theme: str = "system"  # one of app.core.constants.THEMES
    preferred_language: str = "en"  # one of app.core.constants.LANGUAGES
    # Customer-side privacy (ask 11) — same values and meaning as on the
    # artisan profile, applied wherever this user's phone/area is shown to
    # the other party of a booking.
    phone_visibility: str = "after_escrow"
    share_neighborhood: bool = True
    # Notification preferences (ask 12).
    push_enabled: bool = True
    email_summaries: bool = True
    # Terms acceptance captured at signup (ask 38).
    terms_version: Optional[str] = None
    terms_accepted_at: Optional[datetime] = None
    # Hashed single-use 2FA recovery codes (ask 10).
    two_factor_backup_codes: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

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
    first_name: Name
    last_name: Name
    email: EmailStr
    password: Password
    phone_number: Phone
    nin: Optional[str] = Field(default=None, pattern=r"^\d{11}$")
    state: State
    role: str
    terms_version: str = Field(
        min_length=1,
        max_length=40,
        description="Version of the Terms of Service the user ticked at signup, e.g. '2026-09-01'.",
    )


class UserUpdate(BaseModel):
    first_name: Optional[Name] = None
    last_name: Optional[Name] = None
    phone_number: Optional[Phone] = None
    state: Optional[State] = None
    nin: Optional[str] = Field(default=None, pattern=r"^\d{11}$")
    profile_picture: Optional[str] = None
    theme: Optional[Theme] = None
    preferred_language: Optional[Language] = None
    phone_visibility: Optional[PhoneVisibility] = None
    share_neighborhood: Optional[bool] = None


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
    roles: List[str] = Field(default_factory=list)
    is_admin: bool = False
    is_active: bool
    is_email_verified: bool
    is_paused: bool = Field(
        default=False, description="True while the account is frozen via /auth/freeze-me."
    )
    two_factor_enabled: bool = False
    profile_picture: Optional[str] = None
    theme: str = "system"
    preferred_language: str = "en"
    phone_visibility: PhoneVisibility = "after_escrow"
    share_neighborhood: bool = True
    terms_version: Optional[str] = None
    terms_accepted_at: Optional[datetime] = None
    created_at: datetime
