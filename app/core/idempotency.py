# app/core/idempotency.py
"""Idempotency-Key handling for money/escrow-touching POSTs (spec §5.0).

Endpoints that must not be double-executed on retry (booking creation,
fund-escrow, confirm-completion, dispute, admin refund/release) depend on
`require_idempotency_key` and wrap their body with `get_cached_response` /
`cache_response`.
"""
from typing import Optional

from fastapi import HTTPException, Request, status

from app.models.idempotency_key import IdempotencyRecord
from app.models.user import User


async def require_idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required for this operation.",
        )
    return key


async def get_cached_response(key: str, user: User, endpoint: str) -> Optional[dict]:
    existing = await IdempotencyRecord.find_one(
        {"key": key, "user.$id": user.id, "endpoint": endpoint}
    )
    return existing.response_body if existing else None


async def cache_response(
    key: str, user: User, endpoint: str, response_body: dict, response_status: int
) -> None:
    record = IdempotencyRecord(
        key=key,
        user=user,
        endpoint=endpoint,
        response_status=response_status,
        response_body=response_body,
    )
    try:
        await record.insert()
    except Exception:
        # Lost a race to store the same (key, user, endpoint) — the record
        # that won is the one future replays will see, which is correct.
        pass
