# app/services/paystack.py
"""Thin async wrapper around the Paystack API (spec §11) — the one place
that talks to Paystack, so deposit init, NUBAN verification, and payouts all
share the same auth header and error handling instead of each reimplementing
the httpx call.
"""
import logging
from typing import Optional

import httpx

from app.core.config import settings

BASE_URL = "https://api.paystack.co"
logger = logging.getLogger("kazihub.paystack")


class PaystackError(Exception):
    """`safe_to_expose` distinguishes a business error Paystack itself
    returned (e.g. "Invalid account number" — fine to show the caller) from
    a transport/network failure (connection refused, DNS, timeout — could
    embed internal hostnames/paths and must never reach an API response;
    security-review fix). Callers should only interpolate this exception's
    message into a client-facing detail when `safe_to_expose` is True.
    """

    def __init__(self, message: str, response_data: Optional[dict] = None, safe_to_expose: bool = True):
        super().__init__(message)
        self.response_data = response_data or {}
        self.safe_to_expose = safe_to_expose

    def client_message(self) -> str:
        return str(self) if self.safe_to_expose else "The payment provider is temporarily unavailable."


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
        logger.error("Paystack transport error calling %s: %s", path, e)
        raise PaystackError("Network error contacting Paystack", safe_to_expose=False)

    if not data.get("status"):
        raise PaystackError(data.get("message", "Paystack request failed"), data, safe_to_expose=True)
    return data


async def initialize_transaction(
    email: str, amount_kobo: int, reference: str, metadata: dict, callback_url: Optional[str] = None
) -> dict:
    body = {"email": email, "amount": amount_kobo, "reference": reference, "metadata": metadata}
    if callback_url:
        body["callback_url"] = callback_url
    data = await _request("POST", "/transaction/initialize", json=body)
    return data["data"]


async def list_banks() -> list:
    """Every Nigerian bank Paystack can pay into (follows its cursor
    pagination)."""
    banks, cursor = [], None
    for _ in range(20):
        params = {"country": "nigeria", "currency": "NGN", "perPage": 100, "use_cursor": "true"}
        if cursor:
            params["next"] = cursor
        data = await _request("GET", "/bank", params=params)
        banks.extend(data.get("data") or [])
        cursor = (data.get("meta") or {}).get("next")
        if not cursor:
            break
    return banks


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
