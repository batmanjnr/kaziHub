import random
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import create_access_token, get_password_hash, verify_password
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
from app.services.email import send_otp_email

router = APIRouter()


def generate_otp() -> str:
    """Generate a 5-digit numeric string."""
    return str(random.randint(10000, 99999))


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(user_in: UserCreate, background_tasks: BackgroundTasks):
    """Sign up a new user and send a 5-digit OTP via Mailtrap."""
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

    user = User(
        first_name=user_in.first_name,
        last_name=user_in.last_name,
        email=user_in.email,
        phone_number=user_in.phone_number,
        nin=user_in.nin,
        state=user_in.state,
        role=user_in.role.lower(),
        hashed_password=get_password_hash(user_in.password),
        is_email_verified=False,
        otp_code=otp,
        otp_expires_at=otp_expiry,
    )
    await user.insert()

    # Automatically initialize empty profile document for the user
    profile = Profile(user_id=user.id)
    await profile.insert()

    # Dispatch email asynchronously in background
    background_tasks.add_task(send_otp_email, user.email, otp)

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
        created_at=user.created_at,
    )


@router.post("/verify-email", status_code=status.HTTP_200_OK)
async def verify_email(payload: VerifyEmailSchema):
    """Verify 5-digit OTP sent to user's email."""
    user = await User.find_one(User.email == payload.email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found."
        )

    if user.is_email_verified:
        return {"detail": "Email is already verified."}

    if user.otp_code != payload.otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP code."
        )

    # Timezone-safe comparison
    now = datetime.now(timezone.utc)
    expires_at = user.otp_expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at is None or now > expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP code has expired. Please request a new one.",
        )

    # Mark email as verified and clear OTP fields
    user.is_email_verified = True
    user.otp_code = None
    user.otp_expires_at = None
    await user.save()

    return {"detail": "Email verified successfully. You can now log in."}


@router.post("/resend-otp", status_code=status.HTTP_200_OK)
async def resend_otp(payload: ResendOTPSchema, background_tasks: BackgroundTasks):
    """Resend a fresh 5-digit OTP code to user's email."""
    user = await User.find_one(User.email == payload.email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found."
        )

    if user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email is already verified.",
        )

    # Overwrite old OTP with a new code and fresh expiration window
    otp = generate_otp()
    user.otp_code = otp
    user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    await user.save()

    background_tasks.add_task(send_otp_email, user.email, otp)

    return {"detail": "A new 5-digit OTP has been sent to your email."}


@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """Authenticate user and return access token if email is verified."""
    user = await User.find_one(User.email == form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email address not verified. Please verify your email with the OTP sent to your inbox.",
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