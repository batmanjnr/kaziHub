# app/api/deps.py
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
import jwt
from beanie import PydanticObjectId

from app.core.config import settings
from app.core.errors import APIError
from app.core.time import utc_now
from app.models.profile import Profile
from app.models.session import UserSession
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# What a frozen account may still do (frontend ask 17): get back out.
# Matched against the path below /api/v1. Everything else that changes
# data is refused while the account is frozen.
FROZEN_ALLOWED_PATHS = (
    "/auth/unfreeze-me",
    "/auth/freeze-me",  # reaches its own clearer 409 "already frozen"
    "/auth/change-password",
    "/auth/logout",
    "/auth/revoke-sessions",
    "/auth/sessions/",
)


def _frozen_request_allowed(request: Request) -> bool:
    if request.method in SAFE_METHODS:
        return True
    path = request.url.path
    prefix = "/api/v1"
    if path.startswith(prefix):
        path = path[len(prefix):]
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in FROZEN_ALLOWED_PATHS)


def frozen_account_error() -> APIError:
    return APIError(status.HTTP_423_LOCKED, "Your account is frozen.", code="account_frozen")


async def get_current_user(request: Request, token: str = Depends(oauth2_scheme)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        user_id: str = payload.get("sub")
        token_version = payload.get("token_version")
        if user_id is None or token_version is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    user = await User.get(PydanticObjectId(user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    # Immediate invalidation: a suspended account's still-unexpired access
    # token is rejected as soon as an admin bumps token_version, rather than
    # waiting up to 15 minutes for natural expiry (spec §3).
    if user.token_version != token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been invalidated. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # A device that was signed out (logout, DELETE /auth/sessions/{id}) is
    # cut off at once, not when its access token expires (ask 19). Tokens
    # minted before the `sid` claim existed skip this check until they
    # expire (15 minutes).
    sid = payload.get("sid")
    if sid is not None:
        live_session = await UserSession.find_one(
            {"family_id": sid, "is_revoked": False, "expires_at": {"$gt": utc_now()}}
        )
        if not live_session:
            raise APIError(
                status.HTTP_401_UNAUTHORIZED,
                "This session was signed out.",
                code="session_signed_out",
                headers={"WWW-Authenticate": "Bearer"},
            )
    request.state.session_id = sid

    if user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account no longer exists.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active or user.is_frozen:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is suspended."
        )

    # One shared check for every authenticated endpoint that changes data,
    # read from the database on each request (ask 17).
    if user.is_paused and not _frozen_request_allowed(request):
        raise frozen_account_error()

    return user


async def get_current_artisan(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure the authenticated user has the 'artisan' role."""
    if current_user.role.lower() != "artisan":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only accounts with the 'artisan' role can create or manage an artisan profile.",
        )
    return current_user


async def get_current_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure the authenticated user carries the is_admin flag and has TOTP
    2FA enabled. Spec §2/§3: 2FA is enforced server-side for any is_admin
    account, not merely offered — an admin who hasn't completed 2FA setup
    (POST /auth/2fa/setup then /auth/2fa/verify) can't use any admin
    endpoint, not even read-only ones.

    Money/status-mutating admin actions layer a second check on top of this
    — a fresh TOTP code per call via app.core.two_factor.verify_admin_totp —
    since being *enabled* isn't the same as being *re-proven* for a
    high-stakes action.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required.",
        )
    if not current_user.two_factor_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin accounts must enable 2FA (POST /auth/2fa/setup) before use.",
        )
    return current_user


async def get_own_artisan_profile(
    current_artisan: User = Depends(get_current_artisan),
) -> Profile:
    """Resolve the artisan_profiles row for the logged-in artisan, used by
    every endpoint that manages a catalog entity owned by that profile
    (gigs, services, portfolio items)."""
    profile = await Profile.find_one({"user.$id": current_artisan.id})
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Create your artisan profile before managing this resource.",
        )
    return profile
