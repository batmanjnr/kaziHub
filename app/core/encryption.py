# app/core/encryption.py
"""Application-layer field encryption for PII (NDPR §12).

Uses AES-256-GCM with a key sourced from settings (a stand-in for a secrets
manager in local/dev; swap PII_ENCRYPTION_KEY for a KMS-backed value in prod).
Encrypted values are stored as raw bytes: 12-byte nonce || ciphertext || tag.
"""
import base64
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

_NONCE_SIZE = 12


def _get_key() -> bytes:
    key = base64.b64decode(settings.PII_ENCRYPTION_KEY)
    if len(key) != 32:
        raise ValueError("PII_ENCRYPTION_KEY must decode to exactly 32 bytes for AES-256-GCM")
    return key


def encrypt_str(plaintext: str) -> bytes:
    """Encrypt a plaintext string, returning nonce||ciphertext bytes for storage."""
    aesgcm = AESGCM(_get_key())
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def decrypt_str(blob: bytes) -> str:
    """Decrypt bytes produced by encrypt_str back into the original string."""
    aesgcm = AESGCM(_get_key())
    nonce, ciphertext = blob[:_NONCE_SIZE], blob[_NONCE_SIZE:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


def mask_tail(plaintext: str, visible: int = 4) -> str:
    """Return a display-safe mask (e.g. '********1234') for a sensitive identifier."""
    if len(plaintext) <= visible:
        return "*" * len(plaintext)
    return "*" * (len(plaintext) - visible) + plaintext[-visible:]


def encrypt_optional(plaintext: Optional[str]) -> Optional[bytes]:
    return encrypt_str(plaintext) if plaintext else None


def decrypt_optional(blob: Optional[bytes]) -> Optional[str]:
    return decrypt_str(blob) if blob else None
