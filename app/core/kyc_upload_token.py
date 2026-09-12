# app/core/kyc_upload_token.py
"""Binds a Cloudinary upload to the user who made it (security-audit fix).

POST /verification/upload previously returned a bare Cloudinary public_id,
and POST /verification/submit accepted whatever public_id the client sent
with no check that the caller was the one who uploaded it. Since
generate_signed_kyc_url's signed download URLs embed the public_id in
plaintext (necessarily — Cloudinary needs it to build the signature), any
public_id an admin or the applicant themselves has ever seen (browser
history, a screenshot, a compromised admin session) could otherwise be
replayed by a different account to claim someone else's ID photo/selfie as
their own KYC submission.

Instead, /upload returns a short-lived signed token binding
(user_id, public_id) together; /submit verifies that token rather than
trusting the public_id alone.

Signed with its own dedicated KYC_UPLOAD_TOKEN_SECRET, not SECRET_KEY
(security-review fix) — reusing the JWT-signing secret here would mean a
single leaked key lets an attacker both forge access tokens and forge KYC
upload-ownership tokens; scoping each purpose to its own key limits the
blast radius of either one leaking alone.
"""
import hmac
import hashlib
import time
from typing import Optional

from app.core.config import settings

DEFAULT_MAX_AGE_SECONDS = 3600


def _sign(payload: str) -> str:
    return hmac.new(
        settings.KYC_UPLOAD_TOKEN_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sign_kyc_upload(user_id: str, public_id: str) -> str:
    timestamp = str(int(time.time()))
    payload = f"{user_id}:{public_id}:{timestamp}"
    return f"{payload}:{_sign(payload)}"


def verify_kyc_upload_token(
    token: str, user_id: str, public_id: str, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
) -> bool:
    parts = token.split(":")
    if len(parts) != 4:
        return False
    token_user_id, token_public_id, timestamp, signature = parts

    if token_user_id != user_id or token_public_id != public_id:
        return False

    payload = f"{token_user_id}:{token_public_id}:{timestamp}"
    if not hmac.compare_digest(signature, _sign(payload)):
        return False

    try:
        age = time.time() - int(timestamp)
    except ValueError:
        return False
    return 0 <= age <= max_age_seconds
