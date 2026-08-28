import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import create_access_token, get_password_hash, verify_password
from app.models.pending_user import PendingUser
from app.models.profile import Profile
from app.models.user import (
    ResendOTPSchema,
    Token,
    User,
    UserCreate,
    UserResponse,
    UserUpdate,
    VerifyEmailSchema,
)
from app.schemas.auth import ForgotPasswordSchema, ResetPasswordSchema
from app.services.email import send_otp_email, send_password_reset_email

router = APIRouter()

UPLOAD_DIR = "static/uploads/profiles"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def generate_otp() -> str:
    """Generate a 5-digit numeric string."""
    return str(random.randint(10000, 99999))


def build_user_response(user: User) -> UserResponse:
    """Safely construct a UserResponse model including avatar and preferences."""
    return UserResponse(
        id=str(user.id),
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        phone_number=user.phone_number,
        nin=user.nin,
        state=user.state,
        role=user.role,
        is_active=user.is_active,
        is_email_verified=user.is_email_verified,
        profile_picture=user.profile_picture,
        theme=getattr(user, "theme", "system"),
        preferred_language=getattr(user, "preferred_language", "en"),
        created_at=user.created_at,
    )


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

    # Check if there's an existing pending registration for this email
    pending_user = await PendingUser.find_one(PendingUser.email == user_in.email)

    if pending_user:
        # Update existing pending record with new details and fresh OTP
        pending_user.first_name = user_in.first_name
        pending_user.last_name = user_in.last_name
        pending_user.phone_number = user_in.phone_number
        pending_user.nin = user_in.nin
        pending_user.state = user_in.state
        pending_user.role = user_in.role.lower()
        pending_user.hashed_password = get_password_hash(user_in.password)
        pending_user.otp_code = otp
        pending_user.otp_expires_at = otp_expiry
        await pending_user.save()
    else:
        # Create new staged pending user document
        pending_user = PendingUser(
            first_name=user_in.first_name,
            last_name=user_in.last_name,
            email=user_in.email,
            phone_number=user_in.phone_number,
            nin=user_in.nin,
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP code."
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

    # 1. Create official User document in main collection
    user = User(
        first_name=pending_user.first_name,
        last_name=pending_user.last_name,
        email=pending_user.email,
        phone_number=pending_user.phone_number,
        nin=pending_user.nin,
        state=pending_user.state,
        role=pending_user.role,
        hashed_password=pending_user.hashed_password,
        is_email_verified=True,
        created_at=pending_user.created_at,
    )
    await user.insert()

    # 2. Initialize official Profile document
    profile = Profile(
        user=user,
        state=user.state,
        category="",
    )
    await profile.insert()

    # 3. Clean up staging record
    await pending_user.delete()

    return build_user_response(user)


@router.post("/resend-otp", status_code=status.HTTP_200_OK)
async def resend_otp(payload: ResendOTPSchema, background_tasks: BackgroundTasks):
    """Resend a fresh 5-digit OTP code to a pending user."""
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
    await pending_user.save()

    background_tasks.add_task(send_otp_email, pending_user.email, otp)

    return {"detail": "A new 5-digit OTP has been sent to your email."}


@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """Authenticate user and return access token."""
    user = await User.find_one(User.email == form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive account",
        )

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(user.id)}, expires_delta=access_token_expires
    )

    return {"access_token": access_token, "token_type": "bearer"}


# ==========================================
# PASSWORD RESET ENDPOINTS
# ==========================================

@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(
    payload: ForgotPasswordSchema, background_tasks: BackgroundTasks
):
    """Request a password reset OTP sent to email."""
    user = await User.find_one(User.email == payload.email)

    # Return generic response even if email doesn't exist to prevent email enumeration
    if not user:
        return {
            "detail": "If an account with that email exists, a password reset code has been sent."
        }

    otp = generate_otp()
    user.reset_otp_code = otp
    user.reset_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid reset code.",
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
    await user.save()

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

    if "password" in update_data:
        password = update_data.pop("password")
        if password:
            current_user.hashed_password = get_password_hash(password)

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

    await current_user.save()
    return build_user_response(current_user)


@router.post("/me/picture", response_model=UserResponse)
async def upload_user_profile_picture(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload a profile picture directly for the logged-in user."""
    allowed_types = ["image/jpeg", "image/png", "image/webp"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPEG, PNG, or WEBP image formats are supported.",
        )

    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    unique_filename = f"{uuid.uuid4().hex}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_filename)

    with open(file_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)

    relative_path = f"/static/uploads/profiles/{unique_filename}"
    current_user.profile_picture = relative_path
    await current_user.save()

    # Sync image with user's Profile document if one exists
    profile = await Profile.find_one({"user.$id": current_user.id})
    if profile:
        await profile.set({"profile_picture": relative_path})

    return build_user_response(current_user)


@router.delete("/me", status_code=status.HTTP_200_OK)
async def delete_user_me(current_user: User = Depends(get_current_user)):
    """Soft-delete / deactivate account for the currently logged-in user."""
    current_user.is_active = False
    await current_user.save()
    return {"detail": "Account successfully deactivated."}