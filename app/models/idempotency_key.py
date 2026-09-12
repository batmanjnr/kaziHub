# app/models/idempotency_key.py
"""Stores the response for a money/escrow-touching POST keyed by its
Idempotency-Key header, so a duplicate request replays the stored response
instead of re-executing the action (spec §5.0)."""
from datetime import datetime
from beanie import Document, Link
from pymongo import IndexModel

from app.models.user import User


class IdempotencyRecord(Document):
    key: str
    user: Link[User]
    endpoint: str
    response_status: int
    response_body: dict
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "idempotency_records"
        indexes = [
            IndexModel([("key", 1), ("user", 1), ("endpoint", 1)], unique=True),
        ]
