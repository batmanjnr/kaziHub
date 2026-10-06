import os
import random
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import uuid4

import pyotp
from beanie import PydanticObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.cloudinary import delete_file_from_cloudinary, upload_file_to_cloudinary
from app.core.encryption import decrypt_optional, encrypt_optional, encrypt_str, mask_tail
from app.core.nin_hash import compute_nin_hash
from app.core.constants import LANGUAGE_LABEL_PREFIXES, LANGUAGES
from app.core.errors import APIError
from app.core.rate_limit import get_client_ip, rate_limiter
from app.core.time import utc_now
from app.core.two_factor import (
    generate_backup_codes,
    totp_code_is_valid,
    verify_totp_or_backup_code,
)
from app.core.upload_validation import IMAGE_TYPES, validate_upload
from app.services.moderation import moderate_image
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password_async,
    hash_refresh_token,
    verify_password_async,
)
from app.models.pending_user import PendingUser
from app.models.profile import Profile
from app.models.session import UserSession
from app.models.user import (
    ResendOTPSchema,
    User,
    UserCreate,
    UserResponse,
    UserUpdate,
    VerifyEmailSchema,
)
from app.models.user_role import UserRole
from app.schemas.auth import (
    ChangePasswordSchema,
    ConfirmEmailChangeSchema,
    ForgotPasswordSchema,
    LogoutSchema,
    RequestEmailChangeSchema,
    ResetPasswordSchema,
    TwoFactorDisableSchema,
)
from app.services.email import send_email_change_otp, send_otp_email, send_password_reset_email

router = APIRouter()

UPLOAD_DIR = "static/uploads/profiles"
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_OTP_ATTEMPTS = 3


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


def generate_otp() -> str:
    """Generate a 5-digit numeric string."""
    return str(random.randint(10000, 99999))


def normalize_language(value: Optional[str]) -> str:
    """Older accounts hold full labels like "English (Nigeria)"; always
    report a code from LANGUAGES (ask 20)."""
    if value in LANGUAGES:
        return value
    lowered = (value or "").strip().lower()
    for prefix, code in LANGUAGE_LABEL_PREFIXES.items():
        if lowered.startswith(prefix):
            return code
    return "en"


def build_user_response(user: User) -> UserResponse:
    """Safely construct a UserResponse model including avatar and preferences."""
    nin_plain = decrypt_optional(user.nin_encrypted)
    return UserResponse(
        id=str(user.id),
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        phone_number=user.phone_number,
        nin_masked=mask_tail(nin_plain) if nin_plain else None,
        state=user.state,
        role=user.role,
        roles=user.roles,
        is_admin=user.is_admin,
        is_active=user.is_active,
        is_email_verified=user.is_email_verified,
        is_paused=user.is_paused,
        two_factor_enabled=user.two_factor_enabled,
        profile_picture=user.profile_picture,
        theme=user.theme if user.theme in ("light", "dark", "system") else "system",
        preferred_language=normalize_language(user.preferred_language),
        phone_visibility=user.phone_visibility,
        share_neighborhood=user.share_neighborhood,
        terms_version=user.terms_version,
        terms_accepted_at=user.terms_accepted_at,
        created_at=user.created_at,
    )


async def issue_token_pair(
    user: User, family_id: Optional[str] = None, request: Optional[Request] = None
) -> TokenPair:
    """Issue a new access/refresh pair. Passing `family_id` continues an
    existing rotation chain (refresh); omitting it starts a new one (login).

    The family id doubles as the session id: it's the access token's `sid`
    claim (asks 9 and 19), stable across refreshes of the same login."""
    family_id = family_id or str(uuid4())
    access_token = create_access_token(
        data={
            "sub": str(user.id),
            "sid": family_id,
            "roles": user.roles,
            "is_admin": user.is_admin,
            "token_version": user.token_version,
        },
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    refresh_token = generate_refresh_token()
    now = datetime.now(timezone.utc)
    session = UserSession(
        user=user,
        refresh_token_hash=hash_refresh_token(refresh_token),
        family_id=family_id,
        expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        user_agent=request.headers.get("user-agent") if request else None,
        ip_address=get_client_ip(request) if request else None,
        last_used_at=now,
    )
    await session.insert()

    return TokenPair(access_token=access_token, refresh_token=refresh_token)


async def revoke_all_sessions_for(user: User) -> None:
    await UserSession.find({"user.$id": user.id, "is_revoked": False}).update(
        {"$set": {"is_revoked": True, "revoked_reason": "signed_out"}}
    )


async def deactivate_account(user: User) -> None:
    user.is_active = False
    user.deleted_at = datetime.now(timezone.utc)
    user.token_version += 1
    await user.save()
    await revoke_all_sessions_for(user)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(request: Request, user_in: UserCreate, background_tasks: BackgroundTasks):
    """Stage a new registration and send OTP without writing to main User DB."""
    # FIX (security review): unrate-limited before — an attacker could spam
    # this endpoint with arbitrary victim emails, using Kazihub's own SMTP
    # sender as a spam relay (each call sends a real OTP email) even though
    # they'd never receive the OTP themselves.
    rate_limiter.hit(f"register:{get_client_ip(request)}", limit=10, window_seconds=3600)

    # Check if email is already registered and verified
    existing_user = await User.find_one(User.email == user_in.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this email already exists.",
        )

    if user_in.role.lower() not in ["client", "artisan"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be either 'client' or 'artisan'.",
        )

    # One account per NIN (anti-fraud): checked here for a fast, clear
    # error; the sparse unique index on User.nin_hash is the authoritative
    # enforcement (verify_email below handles the race if two pending
    # registrations somehow reach it with the same NIN at once).
    nin_hash = compute_nin_hash(user_in.nin) if user_in.nin else None
    if nin_hash and await User.find_one(User.nin_hash == nin_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account already exists for this NIN.",
        )

    otp = generate_otp()
    otp_expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
    now = datetime.now(timezone.utc)
    nin_encrypted = encrypt_optional(user_in.nin)

    # Check if there's an existing pending registration for this email
    pending_user = await PendingUser.find_one(PendingUser.email == user_in.email)

    if pending_user:
        # Update existing pending record with new details and fresh OTP
        pending_user.first_name = user_in.first_name
        pending_user.last_name = user_in.last_name
        pending_user.phone_number = user_in.phone_number
        pending_user.nin_encrypted = nin_encrypted
        pending_user.nin_hash = nin_hash
        pending_user.state = user_in.state
        pending_user.role = user_in.role.lower()
        pending_user.hashed_password = await hash_password_async(user_in.password)
        pending_user.otp_code = otp
        pending_user.otp_expires_at = otp_expiry
        pending_user.otp_attempts = 0
        pending_user.terms_version = user_in.terms_version
        pending_user.terms_accepted_at = now
        await pending_user.save()
    else:
        # Create new staged pending user document
        pending_user = PendingUser(
            first_name=user_in.first_name,
            last_name=user_in.last_name,
            email=user_in.email,
            phone_number=user_in.phone_number,
            nin_encrypted=nin_encrypted,
            nin_hash=nin_hash,
            state=user_in.state,
            role=user_in.role.lower(),
            hashed_password=await hash_password_async(user_in.password),
            otp_code=otp,
            otp_expires_at=otp_expiry,
            terms_version=user_in.terms_version,
            terms_accepted_at=now,
            created_at=now,
        )
        await pending_user.insert()

    # Dispatch email asynchronously
    background_tasks.add_task(send_otp_email, pending_user.email, otp)

    return {
        "detail": "Registration initiated. Please verify the OTP sent to your email to complete signup."
    }


@router.post("/verify-email", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def verify_email(payload: VerifyEmailSchema):
    """Verify OTP and insert User & Profile documents into main DB upon success."""
    pending_user = await PendingUser.find_one(PendingUser.email == payload.email)
    if not pending_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending registration not found or expired. Please sign up again.",
        )

    if pending_user.otp_code != payload.otp:
        pending_user.otp_attempts += 1
        if pending_user.otp_attempts >= MAX_OTP_ATTEMPTS:
            pending_user.otp_code = None
            pending_user.otp_expires_at = None
            await pending_user.save()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Too many failed attempts. Please request a new OTP.",
            )
        await pending_user.save()
        remaining = MAX_OTP_ATTEMPTS - pending_user.otp_attempts
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid OTP code. {remaining} attempt(s) remaining.",
        )

    # Timezone-safe expiration comparison
    now = datetime.now(timezone.utc)
    expires_at = pending_user.otp_expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at is None or now > expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP code has expired. Please request a new one.",
        )

    role = pending_user.role.lower()
    roles = ["client"] + (["artisan"] if role == "artisan" else [])

    # 1. Create official User document in main collection
    user = User(
        first_name=pending_user.first_name,
        last_name=pending_user.last_name,
        email=pending_user.email,
        phone_number=pending_user.phone_number,
        nin_encrypted=pending_user.nin_encrypted,
        nin_hash=pending_user.nin_hash,
        state=pending_user.state,
        role=pending_user.role,
        roles=roles,
        hashed_password=pending_user.hashed_password,
        is_email_verified=True,
        terms_version=pending_user.terms_version,
        terms_accepted_at=pending_user.terms_accepted_at,
        created_at=pending_user.created_at,
    )
    try:
        await user.insert()
    except Exception as e:
        # Race: two pending registrations with the same NIN both reached
        # this point before either completed. The sparse unique index on
        # nin_hash is what actually caught it — surface a clean error
        # instead of a raw 500.
        if "nin_hash" in str(e) and "duplicate key" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An account already exists for this NIN.",
            )
        raise

    # 2. Grant capability rows (spec §2/§4.2) — every account implicitly
    # holds 'client'; 'artisan' is granted alongside it here since this
    # registration flow provisions the role up front.
    for granted_role in roles:
        await UserRole(user=user, role=granted_role).insert()

    # 3. Initialize official Profile document
    profile = Profile(
        user=user,
        state=user.state,
        category="",
    )
    await profile.insert()

    # 4. Clean up staging record
    await pending_user.delete()

    return build_user_response(user)


@router.post("/resend-otp", status_code=status.HTTP_200_OK)
async def resend_otp(payload: ResendOTPSchema, background_tasks: BackgroundTasks):
    """Resend a fresh 5-digit OTP code to a pending user."""
    rate_limiter.hit(f"resend-otp:{payload.email}", limit=3, window_seconds=3600)

    pending_user = await PendingUser.find_one(PendingUser.email == payload.email)
    if not pending_user:
        # Check if user is already fully registered
        existing_user = await User.find_one(User.email == payload.email)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email is already verified.",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending registration not found. Please register first.",
        )

    otp = generate_otp()
    pending_user.otp_code = otp
    pending_user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    pending_user.otp_attempts = 0
    await pending_user.save()

    background_tasks.add_task(send_otp_email, pending_user.email, otp)

    return {"detail": "A new 5-digit OTP has been sent to your email."}


@router.post(
    "/login",
    response_model=TokenPair,
    responses={
        401: {
            "description": (
                "Every 401 carries a machine-readable `code`: `invalid_credentials` (wrong email or "
                "password), `totp_required` (password correct, account has 2FA on, no code sent), or "
                "`totp_invalid` (code wrong or expired). The 2FA codes are only ever returned after the "
                "password has been checked."
            ),
            "content": {"application/json": {"example": {"detail": "Enter your 2FA code to continue.", "code": "totp_required"}}},
        },
        423: {"description": "`account_suspended` or `account_deleted`."},
    },
)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    totp_code: Optional[str] = Form(
        default=None,
        description="6-digit authenticator code, or an unused backup code, when the account has 2FA on.",
    ),
):
    """Authenticate user and return an access/refresh token pair.

    Send `username` (the email), `password` and, for accounts with 2FA on,
    `totp_code`, as form fields. See the 401 response for how a missing or
    wrong 2FA code is reported (frontend ask 41).
    """
    ip = get_client_ip(request)
    rate_limiter.hit(f"login:{ip}:{form_data.username}", limit=5, window_seconds=900)

    user = await User.find_one(User.email == form_data.username)
    if not user or not await verify_password_async(form_data.password, user.hashed_password):
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect email or password",
            code="invalid_credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.deleted_at is not None:
        raise APIError(status.HTTP_423_LOCKED, "Account no longer exists.", code="account_deleted")

    if not user.is_active or user.is_frozen:
        raise APIError(status.HTTP_423_LOCKED, "This account has been suspended.", code="account_suspended")

    if user.two_factor_enabled:
        if not totp_code:
            raise APIError(
                status.HTTP_401_UNAUTHORIZED, "Enter your 2FA code to continue.", code="totp_required"
            )
        if not await verify_totp_or_backup_code(user, totp_code.strip()):
            raise APIError(
                status.HTTP_401_UNAUTHORIZED, "That 2FA code is wrong or has expired.", code="totp_invalid"
            )

    return await issue_token_pair(user, request=request)


@router.post("/refresh", response_model=TokenPair)
async def refresh_token(payload: RefreshRequest, request: Request):
    """Exchange a refresh token for a new pair, rotating it in the process.

    Reuse of an already-rotated (revoked) refresh token is treated as
    evidence of theft: the entire session family is revoked immediately
    (spec §3).
    """
    token_hash = hash_refresh_token(payload.refresh_token)
    session = await UserSession.find_one(UserSession.refresh_token_hash == token_hash)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token."
        )

    if session.is_revoked:
        if session.revoked_reason == "signed_out":
            # The device was signed out on purpose (ask 19) — not theft.
            raise APIError(
                status.HTTP_401_UNAUTHORIZED, "This session was signed out.", code="session_signed_out"
            )
        # A refresh token that was already spent by a rotation being used
        # again: treat as stolen and end that whole login.
        await UserSession.find({"family_id": session.family_id}).update(
            {"$set": {"is_revoked": True}}
        )
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            "Refresh token reuse detected; this login has been signed out for safety.",
            code="refresh_token_reused",
        )

    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired. Please log in again.",
        )

    user_id = session.user.ref.id if hasattr(session.user, "ref") else session.user.id
    user = await User.get(user_id)
    if (
        not user
        or user.deleted_at is not None
        or not user.is_active
        or user.is_frozen
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is not available."
        )

    # Single-use: this token is now spent, whether or not the caller ever
    # sees the new pair.
    session.is_revoked = True
    session.revoked_reason = "rotated"
    await session.save()

    return await issue_token_pair(user, family_id=session.family_id, request=request)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(payload: LogoutSchema):
    """Sign out this device only (ask 8): revokes the session the given
    refresh token belongs to. Its access token stops working at once too.
    Always 204, so it can't be used to probe which tokens exist."""
    session = await UserSession.find_one(
        UserSession.refresh_token_hash == hash_refresh_token(payload.refresh_token)
    )
    if session:
        await UserSession.find({"family_id": session.family_id, "is_revoked": False}).update(
            {"$set": {"is_revoked": True, "revoked_reason": "signed_out"}}
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/revoke-sessions", status_code=status.HTTP_200_OK)
async def revoke_sessions(current_user: User = Depends(get_current_user)):
    """Invalidate all active refresh tokens and access tokens for the user."""
    current_user.token_version += 1
    await current_user.save()
    await revoke_all_sessions_for(current_user)
    return {"detail": "All sessions have been revoked."}


# ==========================================
# PASSWORD RESET ENDPOINTS
# ==========================================

@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(
    request: Request, payload: ForgotPasswordSchema, background_tasks: BackgroundTasks
):
    """Request a password reset OTP sent to email."""
    rate_limiter.hit(f"forgot-password:{payload.email}", limit=3, window_seconds=3600)

    user = await User.find_one(User.email == payload.email)

    # Return generic response even if email doesn't exist to prevent email enumeration
    if not user:
        return {
            "detail": "If an account with that email exists, a password reset code has been sent."
        }

    otp = generate_otp()
    user.reset_otp_code = otp
    user.reset_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    user.reset_otp_attempts = 0
    await user.save()

    background_tasks.add_task(send_password_reset_email, user.email, otp)

    return {
        "detail": "If an account with that email exists, a password reset code has been sent."
    }


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(payload: ResetPasswordSchema):
    """Verify reset OTP and set a new password."""
    user = await User.find_one(User.email == payload.email)
    if not user or not user.reset_otp_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired password reset request.",
        )

    if user.reset_otp_code != payload.otp:
        user.reset_otp_attempts += 1
        if user.reset_otp_attempts >= MAX_OTP_ATTEMPTS:
            user.reset_otp_code = None
            user.reset_otp_expires_at = None
            await user.save()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Too many failed attempts. Please request a new reset code.",
            )
        await user.save()
        remaining = MAX_OTP_ATTEMPTS - user.reset_otp_attempts
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid reset code. {remaining} attempt(s) remaining.",
        )

    # Timezone-safe expiration comparison
    now = datetime.now(timezone.utc)
    expires_at = user.reset_otp_expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at is None or now > expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset code has expired. Please request a new one.",
        )

    # Hash new password and clear reset fields
    user.hashed_password = await hash_password_async(payload.new_password)
    user.reset_otp_code = None
    user.reset_otp_expires_at = None
    user.reset_otp_attempts = 0
    # A password reset invalidates any existing sessions/tokens — if an
    # attacker had a live session, this cuts it off.
    user.token_version += 1
    await user.save()
    await revoke_all_sessions_for(user)

    return {"detail": "Password has been reset successfully. You can now log in."}


# ==========================================
# USER MANAGEMENT ENDPOINTS
# ==========================================

@router.get("/me", response_model=UserResponse)
async def read_user_me(current_user: User = Depends(get_current_user)):
    """Fetch profile details for the currently logged-in user."""
    return build_user_response(current_user)


@router.put("/me", response_model=UserResponse)
async def update_user_me(
    user_in: UserUpdate,
    current_user: User = Depends(get_current_user),
):
    """Update user profile details, theme preferences, or language."""
    update_data = user_in.model_dump(exclude_unset=True)

    if "nin" in update_data:
        nin_value = update_data.pop("nin")
        new_hash = compute_nin_hash(nin_value) if nin_value else None
        if new_hash and new_hash != current_user.nin_hash:
            existing = await User.find_one(User.nin_hash == new_hash)
            if existing and str(existing.id) != str(current_user.id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="An account already exists for this NIN.",
                )
        current_user.nin_encrypted = encrypt_optional(nin_value)
        current_user.nin_hash = new_hash

    # theme, preferred_language, names, phone and state are validated by
    # UserUpdate itself (422 on anything else — asks 20 and 37).

    for field, value in update_data.items():
        if hasattr(current_user, field) and value is not None:
            setattr(current_user, field, value)

    current_user.updated_at = utc_now()
    await current_user.save()

    # Artisans keep a copy of their state on the profile for search.
    if "state" in update_data:
        profile = await Profile.find_one({"user.$id": current_user.id})
        if profile:
            await profile.set({"state": current_user.state})

    return build_user_response(current_user)


@router.post("/me/picture", response_model=UserResponse)
async def upload_user_profile_picture(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload a profile picture directly to Cloudinary for the logged-in user."""
    await validate_upload(file, allowed_types=IMAGE_TYPES)

    # Delete old profile picture if it exists and is a Cloudinary URL
    if current_user.profile_picture and "cloudinary.com" in current_user.profile_picture:
        await delete_file_from_cloudinary(current_user.profile_picture)

    secure_url = await upload_file_to_cloudinary(file, folder="kazihub/profiles")
    if not await moderate_image(secure_url):
        await delete_file_from_cloudinary(secure_url)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This image did not pass content moderation.",
        )

    current_user.profile_picture = secure_url
    await current_user.save()

    # Sync image with user's Profile document if one exists
    profile = await Profile.find_one({"user.$id": current_user.id})
    if profile:
        await profile.set({"profile_picture": secure_url})

    return build_user_response(current_user)


@router.post("/deactivate-me", status_code=status.HTTP_200_OK)
async def deactivate_me(current_user: User = Depends(get_current_user)):
    """Soft-delete the account and revoke all sessions immediately.

    Anonymization of PII runs as a separate background job after the
    business's retention window (spec §12) — see Phase 10.
    """
    await deactivate_account(current_user)
    return {
        "detail": "Account deactivated. Your data will be anonymized after the retention period."
    }


@router.delete("/me", status_code=status.HTTP_200_OK)
async def delete_user_me(current_user: User = Depends(get_current_user)):
    """Alias of /deactivate-me, kept for existing clients."""
    await deactivate_account(current_user)
    return {"detail": "Account successfully deactivated."}


# ==========================================
# TWO-FACTOR AUTH (mandatory for admin accounts — spec §2/§3)
# ==========================================

class TwoFactorSetupResponse(BaseModel):
    secret: str
    otpauth_url: str


class TwoFactorVerifySchema(BaseModel):
    totp_code: str


class TwoFactorEnabledResponse(BaseModel):
    detail: str
    backup_codes: List[str] = Field(
        description="Ten single-use recovery codes. Shown only once — the user should save them."
    )


class BackupCodesRegenerateSchema(BaseModel):
    totp_code: str


@router.post("/2fa/setup", response_model=TwoFactorSetupResponse)
async def setup_two_factor(current_user: User = Depends(get_current_user)):
    """Generate a new TOTP secret. 2FA isn't active until /2fa/verify
    confirms the user can actually produce a valid code from it."""
    secret = pyotp.random_base32()
    current_user.two_factor_secret_encrypted = encrypt_str(secret)
    current_user.two_factor_enabled = False
    await current_user.save()

    totp = pyotp.TOTP(secret)
    otpauth_url = totp.provisioning_uri(name=current_user.email, issuer_name="KaziHub")
    return TwoFactorSetupResponse(secret=secret, otpauth_url=otpauth_url)


@router.post("/2fa/verify", response_model=TwoFactorEnabledResponse)
async def verify_two_factor_setup(
    payload: TwoFactorVerifySchema, current_user: User = Depends(get_current_user)
):
    """Confirm the user can produce a valid code from the secret issued by
    /2fa/setup, activating 2FA on the account."""
    if not current_user.two_factor_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Call /auth/2fa/setup first.",
        )
    secret = decrypt_optional(current_user.two_factor_secret_encrypted)
    totp = pyotp.TOTP(secret)
    if not totp.verify(payload.totp_code, valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid 2FA code."
        )

    codes, hashes = generate_backup_codes()
    current_user.two_factor_enabled = True
    current_user.two_factor_backup_codes = hashes
    await current_user.save()
    return TwoFactorEnabledResponse(detail="Two-factor authentication enabled.", backup_codes=codes)


@router.post("/2fa/disable", status_code=status.HTTP_200_OK)
async def disable_two_factor(
    payload: TwoFactorDisableSchema, current_user: User = Depends(get_current_user)
):
    """Turn 2FA off (ask 10). Needs the current password and either an
    authenticator code or an unused backup code, so a user who lost their
    authenticator can still get back in and turn it off."""
    if not current_user.two_factor_enabled:
        raise APIError(status.HTTP_409_CONFLICT, "Two-factor authentication is already off.", code="totp_not_enabled")
    if not await verify_password_async(payload.current_password, current_user.hashed_password):
        raise APIError(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.", code="invalid_password")
    if not await verify_totp_or_backup_code(current_user, payload.totp_code.strip()):
        raise APIError(status.HTTP_400_BAD_REQUEST, "That 2FA code is wrong or has expired.", code="totp_invalid")
    if current_user.is_admin:
        raise APIError(
            status.HTTP_403_FORBIDDEN, "Admin accounts must keep 2FA on.", code="totp_required_for_admin"
        )

    current_user.two_factor_enabled = False
    current_user.two_factor_secret_encrypted = None
    current_user.two_factor_backup_codes = []
    await current_user.save()
    return {"detail": "Two-factor authentication disabled."}


@router.post("/2fa/backup-codes", response_model=TwoFactorEnabledResponse)
async def regenerate_backup_codes(
    payload: BackupCodesRegenerateSchema, current_user: User = Depends(get_current_user)
):
    """Replace all backup codes with ten new ones (old ones stop working).
    Needs a current authenticator code."""
    if not current_user.two_factor_enabled:
        raise APIError(status.HTTP_409_CONFLICT, "Two-factor authentication is off.", code="totp_not_enabled")
    if not totp_code_is_valid(current_user, payload.totp_code.strip()):
        raise APIError(status.HTTP_400_BAD_REQUEST, "That 2FA code is wrong or has expired.", code="totp_invalid")
    codes, hashes = generate_backup_codes()
    current_user.two_factor_backup_codes = hashes
    await current_user.save()
    return TwoFactorEnabledResponse(detail="New backup codes issued.", backup_codes=codes)


# ==========================================
# CHANGE PASSWORD
# ==========================================

@router.post("/change-password", status_code=status.HTTP_200_OK)
async def change_password(
    payload: ChangePasswordSchema, current_user: User = Depends(get_current_user)
):
    """Change the password for an already-authenticated user (current +
    new password), distinct from the forgot/reset-password OTP flow."""
    if not await verify_password_async(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect."
        )
    current_user.hashed_password = await hash_password_async(payload.new_password)
    # Same rationale as reset-password: a password change invalidates any
    # other live sessions in case the old password had leaked.
    current_user.token_version += 1
    await current_user.save()
    await revoke_all_sessions_for(current_user)

    return {"detail": "Password changed successfully. Please log in again."}


# ==========================================
# ACTIVE DEVICES & SESSIONS
# ==========================================

class SessionResponse(BaseModel):
    id: str
    session_id: str = Field(description="Stable id of this login; matches the access token's `sid` claim.")
    is_current: bool = Field(description="True for the session making this request.")
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime


@router.get("/sessions", response_model=List[SessionResponse])
async def list_sessions(request: Request = None, current_user: User = Depends(get_current_user)):
    """List this user's active (non-revoked, unexpired) refresh-token
    sessions, so the client can render 'Active Devices & Sessions'.
    `is_current` marks the device making the call (ask 9)."""
    current_sid = getattr(request.state, "session_id", None) if request else None
    now = datetime.now(timezone.utc)
    sessions = await UserSession.find(
        {"user.$id": current_user.id, "is_revoked": False, "expires_at": {"$gt": now}}
    ).sort("-last_used_at").to_list()
    return [
        SessionResponse(
            id=str(s.id),
            session_id=s.family_id,
            is_current=s.family_id == current_sid,
            user_agent=s.user_agent,
            ip_address=s.ip_address,
            created_at=s.created_at,
            last_used_at=s.last_used_at,
            expires_at=s.expires_at,
        )
        for s in sessions
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_200_OK)
async def revoke_session(session_id: str, current_user: User = Depends(get_current_user)):
    """Revoke a single session by id (e.g. 'log out that device')."""
    try:
        session = await UserSession.get(PydanticObjectId(session_id))
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid session ID.")

    session_owner_id = (
        session.user.ref.id if session and hasattr(session.user, "ref") else (session.user.id if session else None)
    )
    if not session or str(session_owner_id) != str(current_user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")

    await UserSession.find({"family_id": session.family_id, "is_revoked": False}).update(
        {"$set": {"is_revoked": True, "revoked_reason": "signed_out"}}
    )
    return {"detail": "Session revoked."}


# ==========================================
# FREEZE / UNFREEZE ACCOUNT (self-service, reversible)
# ==========================================

@router.post("/freeze-me", status_code=status.HTTP_200_OK)
async def freeze_me(current_user: User = Depends(get_current_user)):
    """Pause the account: drops any artisan profile out of search and
    blocks new bookings, but — unlike /deactivate-me — login still works,
    which is how the user reverses this via /unfreeze-me.

    While frozen, every endpoint that changes data answers 423
    `account_frozen` except sign-in, refresh, password change, session
    management, sign-out and /unfreeze-me (ask 17). Applies to every role.
    """
    if current_user.is_paused:
        raise APIError(status.HTTP_409_CONFLICT, "Your account is already frozen.", code="already_frozen")
    current_user.is_paused = True
    await current_user.save()

    profile = await Profile.find_one({"user.$id": current_user.id})
    if profile:
        await profile.set({"is_paused": True})

    return {"detail": "Account frozen. Log in and call /auth/unfreeze-me to reverse this."}


@router.post("/unfreeze-me", status_code=status.HTTP_200_OK)
async def unfreeze_me(current_user: User = Depends(get_current_user)):
    if not current_user.is_paused:
        raise APIError(status.HTTP_409_CONFLICT, "Your account isn't frozen.", code="not_frozen")
    current_user.is_paused = False
    await current_user.save()

    profile = await Profile.find_one({"user.$id": current_user.id})
    if profile:
        await profile.set({"is_paused": False})

    return {"detail": "Account unfrozen."}


# ==========================================
# CHANGE EMAIL (security-sensitive: OTP-gated to the new address)
# ==========================================

@router.post("/change-email", status_code=status.HTTP_200_OK)
async def request_email_change(
    payload: RequestEmailChangeSchema,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """Start an email change: verifies the current password, then sends an
    OTP to the NEW address. The email only actually changes once that OTP
    is confirmed via /change-email/confirm, proving the user controls it."""
    if not await verify_password_async(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect."
        )

    existing = await User.find_one(User.email == payload.new_email)
    if existing and str(existing.id) != str(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="That email is already in use."
        )

    otp = generate_otp()
    current_user.pending_email = payload.new_email
    current_user.email_change_otp = otp
    current_user.email_change_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    current_user.email_change_otp_attempts = 0
    await current_user.save()

    background_tasks.add_task(send_email_change_otp, payload.new_email, otp)

    return {"detail": "Confirmation code sent to the new email address."}


@router.post("/change-email/confirm", response_model=UserResponse)
async def confirm_email_change(
    payload: ConfirmEmailChangeSchema, current_user: User = Depends(get_current_user)
):
    if not current_user.pending_email or not current_user.email_change_otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending email change. Call /auth/change-email first.",
        )

    if current_user.email_change_otp != payload.otp:
        current_user.email_change_otp_attempts += 1
        if current_user.email_change_otp_attempts >= MAX_OTP_ATTEMPTS:
            current_user.pending_email = None
            current_user.email_change_otp = None
            current_user.email_change_otp_expires_at = None
            await current_user.save()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Too many failed attempts. Please request a new code.",
            )
        await current_user.save()
        remaining = MAX_OTP_ATTEMPTS - current_user.email_change_otp_attempts
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid code. {remaining} attempt(s) remaining.",
        )

    now = datetime.now(timezone.utc)
    expires_at = current_user.email_change_otp_expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is None or now > expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Code has expired. Please request a new one.",
        )

    existing = await User.find_one(User.email == current_user.pending_email)
    if existing and str(existing.id) != str(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="That email is already in use."
        )

    current_user.email = current_user.pending_email
    current_user.is_email_verified = True
    current_user.pending_email = None
    current_user.email_change_otp = None
    current_user.email_change_otp_expires_at = None
    current_user.email_change_otp_attempts = 0
    await current_user.save()

    return build_user_response(current_user)
