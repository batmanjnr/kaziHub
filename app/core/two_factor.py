# app/core/two_factor.py
"""TOTP two-factor auth (spec §2/§3, hardened further in a high-assurance
security review).

`verify_totp_code` is the one place that actually checks a code against a
user's secret; both login-time enforcement (any user who has opted in) and
admin re-auth (mandatory, not optional) call it, so there's exactly one
implementation of "is this code valid" to keep correct.
"""
import pyotp
from fastapi import HTTPException, status

from app.core.encryption import decrypt_str
from app.models.user import User


def verify_totp_code(user: User, totp_code: str) -> None:
    if not user.two_factor_enabled or not user.two_factor_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is not enabled on this account.",
        )
    secret = decrypt_str(user.two_factor_secret_encrypted)
    totp = pyotp.TOTP(secret)
    if not totp.verify(totp_code, valid_window=1):
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
