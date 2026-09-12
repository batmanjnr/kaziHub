# app/services/paystack.py
"""Thin async wrapper around the Paystack API (spec §11) — the one place
that talks to Paystack, so deposit init, NUBAN verification, and payouts all
share the same auth header and error handling instead of each reimplementing
the httpx call.
"""
from typing import Optional

import httpx

from app.core.config import settings

BASE_URL = "https://api.paystack.co"


class PaystackError(Exception):
    def __init__(self, message: str, response_data: Optional[dict] = None):
        super().__init__(message)
        self.response_data = response_data or {}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


async def _request(method: str, path: str, **kwargs) -> dict:
    url = f"{BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.request(method, url, headers=_headers(), **kwargs)
            data = res.json()
    except httpx.HTTPError as e:
        raise PaystackError(f"Network error contacting Paystack: {e}")

    if not data.get("status"):
        raise PaystackError(data.get("message", "Paystack request failed"), data)
    return data


async def initialize_transaction(
    email: str, amount_kobo: int, reference: str, metadata: dict
) -> dict:
    data = await _request(
        "POST",
        "/transaction/initialize",
        json={"email": email, "amount": amount_kobo, "reference": reference, "metadata": metadata},
    )
    return data["data"]


async def verify_transaction(reference: str) -> dict:
    data = await _request("GET", f"/transaction/verify/{reference}")
    return data["data"]


async def resolve_account_number(account_number: str, bank_code: str) -> dict:
    """NUBAN resolve — confirms an account number/bank code pair and
    returns the account holder's name (spec §11.5)."""
    data = await _request(
        "GET", "/bank/resolve", params={"account_number": account_number, "bank_code": bank_code}
    )
    return data["data"]


async def create_transfer_recipient(name: str, account_number: str, bank_code: str) -> dict:
    data = await _request(
        "POST",
        "/transferrecipient",
        json={
            "type": "nuban",
            "name": name,
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": "NGN",
        },
    )
    return data["data"]


async def initiate_transfer(
    amount_kobo: int, recipient_code: str, reason: str, reference: str
) -> dict:
    data = await _request(
        "POST",
        "/transfer",
        json={
            "source": "balance",
            "amount": amount_kobo,
            "recipient": recipient_code,
            "reason": reason,
            "reference": reference,
        },
    )
    return data["data"]
