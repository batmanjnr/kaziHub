# app/core/nin_hash.py
"""Blind index for enforcing one-account-per-NIN without ever storing or
indexing the plaintext NIN (anti-fraud requirement).

`nin_encrypted` (AES-256-GCM, random nonce per encryption) is intentionally
*non-deterministic* — the same NIN encrypted twice produces different
ciphertext, so it can never be used to detect a duplicate. `nin_hash` is a
deterministic HMAC-SHA256 of the same NIN under a separate key: the same
real NIN always produces the same hash, so a unique DB index on it catches
a second registration attempt — but the hash cannot be reversed back to the
NIN (HMAC is a one-way function), so nothing readable is exposed by having
it queryable.
"""
import hmac
import hashlib

from app.core.config import settings


def compute_nin_hash(nin: str) -> str:
    return hmac.new(
        settings.NIN_HASH_KEY.encode("utf-8"), nin.encode("utf-8"), hashlib.sha256
    ).hexdigest()
