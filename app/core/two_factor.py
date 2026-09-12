# app/core/two_factor.py
"""TOTP two-factor auth for admin accounts (spec §2/§3).

`two_factor_enabled` is enforced server-side, not just offered: an
`is_admin=True` account without it set can't use anything gated by
`get_current_admin` at all (see app.api.deps). Money/status-mutating admin
actions additionally require a *fresh* TOTP code with each call
(`verify_admin_totp`) — having 2FA enabled isn't enough on its own for those.
"""
import pyotp
from fastapi import HTTPException, status

from app.core.encryption import decrypt_str
from app.models.user import User


def verify_admin_totp(admin: User, totp_code: str) -> None:
    if not admin.two_factor_enabled or not admin.two_factor_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="2FA must be enabled on this admin account before performing this action.",
        )
    secret = decrypt_str(admin.two_factor_secret_encrypted)
    totp = pyotp.TOTP(secret)
    if not totp.verify(totp_code, valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired 2FA code."
        )
