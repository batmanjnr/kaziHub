# app/core/two_factor.py
"""TOTP two-factor auth (spec §2/§3, hardened further in a high-assurance
security review).

`verify_totp_code` is the one place that actually checks a code against a
user's secret; both login-time enforcement (any user who has opted in) and
admin re-auth (mandatory, not optional) call it, so there's exactly one
implementation of "is this code valid" to keep correct.

Backup codes (frontend ask 10) are the recovery route for a lost
authenticator: ten single-use codes issued when 2FA is turned on, stored
only as SHA-256 hashes. Login and /auth/2fa/disable accept one in place of
an authenticator code; admin re-auth for money actions does not.
"""
import hashlib
import secrets
from typing import List, Tuple

import pyotp
from fastapi import HTTPException, status

from app.core.encryption import decrypt_str
from app.models.user import User

BACKUP_CODE_COUNT = 10


def _hash_backup_code(code: str) -> str:
    normalized = code.replace("-", "").replace(" ", "").upper()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_backup_codes() -> Tuple[List[str], List[str]]:
    """Returns (plain codes to show the user once, hashes to store)."""
    codes = [f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}" for _ in range(BACKUP_CODE_COUNT)]
    return codes, [_hash_backup_code(c) for c in codes]


def totp_code_is_valid(user: User, totp_code: str) -> bool:
    if not user.two_factor_enabled or not user.two_factor_secret_encrypted:
        return False
    secret = decrypt_str(user.two_factor_secret_encrypted)
    return pyotp.TOTP(secret).verify(totp_code, valid_window=1)


async def consume_backup_code(user: User, code: str) -> bool:
    """True (and the code is used up) if `code` is one of the user's
    unused backup codes."""
    hashed = _hash_backup_code(code)
    if hashed not in user.two_factor_backup_codes:
        return False
    user.two_factor_backup_codes = [h for h in user.two_factor_backup_codes if h != hashed]
    await user.save()
    return True


async def verify_totp_or_backup_code(user: User, code: str) -> bool:
    return totp_code_is_valid(user, code) or await consume_backup_code(user, code)


def verify_totp_code(user: User, totp_code: str) -> None:
    if not user.two_factor_enabled or not user.two_factor_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is not enabled on this account.",
        )
    if not totp_code_is_valid(user, totp_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired 2FA code."
        )


def verify_admin_totp(admin: User, totp_code: str) -> None:
    """Admin re-auth: 2FA must be enabled at all (spec §2/§3 makes this
    mandatory for admins specifically, unlike regular users for whom it's
    opt-in) — checked before delegating to the shared code-verification
    logic."""
    if not admin.two_factor_enabled or not admin.two_factor_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="2FA must be enabled on this admin account before performing this action.",
        )
    verify_totp_code(admin, totp_code)
