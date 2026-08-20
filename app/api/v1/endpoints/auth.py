# app/api/v1/endpoints/auth.py
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import create_access_token, get_password_hash, verify_password
from app.models.profile import Profile
from app.models.user import Token, User, UserCreate, UserResponse, UserUpdate

router = APIRouter()


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(user_in: UserCreate):
    """Sign up a new user (Client or Artisan)."""
    # Check if email is already registered
    existing_user = await User.find_one(User.email == user_in.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this email already exists.",
        )

    # Validate role input
    if user_in.role.lower() not in ["client", "artisan"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be either 'client' or 'artisan'.",
        )

    # Create new user document
    user = User(
        first_name=user_in.first_name,
        last_name=user_in.last_name,
        email=user_in.email,
        phone_number=user_in.phone_number,
        nin=user_in.nin,
        state=user_in.state,
        role=user_in.role.lower(),
        hashed_password=get_password_hash(user_in.password),
    )
    await user.insert()

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
        created_at=user.created_at,
    )


@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """Authenticate user and return a JWT access token."""
    # OAuth2PasswordRequestForm uses 'username' field for the email
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


@router.get("/me", response_model=UserResponse)
async def get_my_account(current_user: User = Depends(get_current_user)):
    """Get account details for the currently authenticated user."""
    return UserResponse(
        id=str(current_user.id),
        first_name=current_user.first_name,
        last_name=current_user.last_name,
        email=current_user.email,
        phone_number=current_user.phone_number,
        nin=current_user.nin,
        state=current_user.state,
        role=current_user.role,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
    )


@router.patch("/me", response_model=UserResponse)
async def update_my_account(
    user_in: UserUpdate,
    current_user: User = Depends(get_current_user)
):
    """Update primary account details."""
    update_data = user_in.model_dump(exclude_unset=True)
    if update_data:
        await current_user.set(update_data)

    return UserResponse(
        id=str(current_user.id),
        first_name=current_user.first_name,
        last_name=current_user.last_name,
        email=current_user.email,
        phone_number=current_user.phone_number,
        nin=current_user.nin,
        state=current_user.state,
        role=current_user.role,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
    )


@router.delete("/me", status_code=status.HTTP_200_OK)
async def delete_my_account(current_user: User = Depends(get_current_user)):
    """Delete current account and cascade delete associated artisan profile if present."""
    if current_user.role.lower() == "artisan":
        artisan_profile = await Profile.find_one({"user.$id": current_user.id})
        if artisan_profile:
            await artisan_profile.delete()

    await current_user.delete()
    return {"detail": "Account deleted successfully"}