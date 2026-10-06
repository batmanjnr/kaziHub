# app/core/security.py
import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
import bcrypt
import jwt

from app.core.config import settings


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against the hashed password."""
    # Truncate plain password bytes to 72 to match bcrypt's hard limit
    password_bytes = plain_password.encode("utf-8")[:72]
    hashed_bytes = hashed_password.encode("utf-8")
    return bcrypt.checkpw(password_bytes, hashed_bytes)


def get_password_hash(password: str) -> str:
    """Hash a plain password using bcrypt."""
    # Truncate plain password bytes to 72 to prevent bcrypt length errors
    password_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def create_access_token(
    data: dict, expires_delta: Optional[timedelta] = None
) -> str:
    """Generate a signed JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(
        to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return encoded_jwt


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT access token."""
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        return payload
    except jwt.PyJWTError:
        return None


def generate_refresh_token() -> str:
    """A high-entropy opaque token — deliberately not a JWT, since its only
    job is to be looked up by hash in user_sessions."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """Refresh tokens are stored hashed (spec §3) so a DB read alone can't
    be replayed as a live session."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

# bcrypt is deliberately slow CPU work (~0.25s per call on one fast core,
# several seconds on a small shared host). Called directly inside an async
# endpoint it blocks the event loop, freezing every other request until it
# finishes: under a burst of logins the whole API stalls (load test,
# 2026-10-06). Endpoints use these wrappers, which run it in a worker thread.
async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    return await asyncio.to_thread(verify_password, plain_password, hashed_password)


async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(get_password_hash, password)
