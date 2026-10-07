"""Generate a VAPID key pair for Web Push (frontend ask 44).

    python -m scripts.generate_vapid_keys

Put both values in the server's environment (Render: Environment tab) as
VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY, plus VAPID_CLAIMS_EMAIL
(e.g. mailto:support@kazihub.com). Keep the private key secret; changing
the pair later invalidates every existing subscription.
"""
import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


if __name__ == "__main__":
    vapid = Vapid01()
    vapid.generate_keys()
    public = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    print(f"VAPID_PUBLIC_KEY={b64url(public)}")
    print(f"VAPID_PRIVATE_KEY={b64url(private)}")
