import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import pyotp
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.cloudinary import delete_file_from_cloudinary, upload_file_to_cloudinary
from app.core.encryption import decrypt_optional, encrypt_optional, encrypt_str, mask_tail
from app.core.rate_limit import rate_limiter
from app.core.upload_validation import IMAGE_TYPES, validate_upload
from app.services.moderation import moderate_image
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    get_password_hash,
    hash_refresh_token,
    verify_password,
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
from app.schemas.auth import ForgotPasswordSchema, ResetPasswordSchema
from app.services.email import send_otp_email, send_password_reset_email

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


def get_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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
        profile_picture=user.profile_picture,
        theme=getattr(user, "theme", "system"),
        preferred_language=getattr(user, "preferred_language", "en"),
        created_at=user.created_at,
    )


async def issue_token_pair(user: User, family_id: Optional[str] = None) -> TokenPair:
    """Issue a new access/refresh pair. Passing `family_id` continues an
    existing rotation chain (refresh); omitting it starts a new one (login)."""
    access_token = create_access_token(
        data={
            "sub": str(user.id),
            "roles": user.roles,
            "is_admin": user.is_admin,
            "token_version": user.token_version,
        },
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    refresh_token = generate_refresh_token()
    session = UserSession(
        user=user,
        refresh_token_hash=hash_refresh_token(refresh_token),
        family_id=family_id or str(uuid4()),
        expires_at=datetime.now(timezone.utc)
        + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    await session.insert()

    return TokenPair(access_token=access_token, refresh_token=refresh_token)


async def revoke_all_sessions_for(user: User) -> None:
    await UserSession.find({"user.$id": user.id}).update({"$set": {"is_revoked": True}})


async def deactivate_account(user: User) -> None:
    user.is_active = False
    user.deleted_at = datetime.now(timezone.utc)
    user.token_version += 1
    await user.save()
    await revoke_all_sessions_for(user)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(user_in: UserCreate, background_tasks: BackgroundTasks):
    """Stage a new registration and send OTP without writing to main User DB."""
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
        pending_user.state = user_in.state
        pending_user.role = user_in.role.lower()
        pending_user.hashed_password = get_password_hash(user_in.password)
        pending_user.otp_code = otp
        pending_user.otp_expires_at = otp_expiry
        pending_user.otp_attempts = 0
        await pending_user.save()
    else:
        # Create new staged pending user document
        pending_user = PendingUser(
            first_name=user_in.first_name,
            last_name=user_in.last_name,
            email=user_in.email,
            phone_number=user_in.phone_number,
            nin_encrypted=nin_encrypted,
            state=user_in.state,
            role=user_in.role.lower(),
            hashed_password=get_password_hash(user_in.password),
            otp_code=otp,
            otp_expires_at=otp_expiry,
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
        state=pending_user.state,
        role=pending_user.role,
        roles=roles,
        hashed_password=pending_user.hashed_password,
        is_email_verified=True,
        created_at=pending_user.created_at,
    )
    await user.insert()

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


@router.post("/login", response_model=TokenPair)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    """Authenticate user and return an access/refresh token pair."""
    ip = get_client_ip(request)
    rate_limiter.hit(f"login:{ip}:{form_data.username}", limit=5, window_seconds=900)

    user = await User.find_one(User.email == form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Account no longer exists."
        )

    if not user.is_active or user.is_frozen:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive account",
        )

    return await issue_token_pair(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh_token(payload: RefreshRequest):
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
        await UserSession.find({"family_id": session.family_id}).update(
            {"$set": {"is_revoked": True}}
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token reuse detected; all sessions for this login have been revoked.",
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
    await session.save()

    return await issue_token_pair(user, family_id=session.family_id)


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
    user.hashed_password = get_password_hash(payload.new_password)
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
        current_user.nin_encrypted = encrypt_optional(nin_value)

    # Validate theme input if provided
    if "theme" in update_data and update_data["theme"]:
        allowed_themes = ["light", "dark", "system"]
        if update_data["theme"].lower() not in allowed_themes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid theme. Must be one of {allowed_themes}",
            )
        update_data["theme"] = update_data["theme"].lower()

    for field, value in update_data.items():
        if hasattr(current_user, field) and value is not None:
            setattr(current_user, field, value)

    current_user.updated_at = datetime.now(timezone.utc)
    await current_user.save()
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


@router.post("/2fa/verify", status_code=status.HTTP_200_OK)
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

    current_user.two_factor_enabled = True
    await current_user.save()
    return {"detail": "Two-factor authentication enabled."}
