# app/services/jobs/cleanup.py
"""Purges expired OTPs and stale refresh-token sessions (spec §7.2)."""
from datetime import datetime, timedelta, timezone

from app.models.pending_user import PendingUser
from app.models.session import UserSession
from app.models.user import User

SESSION_RETENTION_DAYS = 60


async def cleanup_expired_otps_and_sessions() -> dict:
    now = datetime.now(timezone.utc)

    # Staged registrations whose OTP expired and were never verified — safe
    # to drop; the user just registers again for a fresh OTP.
    deleted_pending = await PendingUser.find({"otp_expires_at": {"$lte": now}}).delete()

    # Password-reset OTPs on real accounts: clear the fields once expired.
    # Never touches the account itself.
    await User.find({"reset_otp_expires_at": {"$ne": None, "$lte": now}}).update(
        {"$set": {"reset_otp_code": None, "reset_otp_expires_at": None, "reset_otp_attempts": 0}}
    )

    # Refresh-token sessions past the retention window, revoked or not.
    cutoff = now - timedelta(days=SESSION_RETENTION_DAYS)
    deleted_sessions = await UserSession.find({"created_at": {"$lte": cutoff}}).delete()

    return {
        "expired_pending_registrations_removed": getattr(deleted_pending, "deleted_count", None),
        "stale_sessions_removed": getattr(deleted_sessions, "deleted_count", None),
    }
