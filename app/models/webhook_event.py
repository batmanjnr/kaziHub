# app/models/webhook_event.py
"""Idempotency guard + retry state for payment-gateway webhooks (spec
§4.17/§7.4/§11). The unique (gateway, event_id) index is used as an atomic
claim: whichever delivery of a given event inserts first owns processing,
so concurrent/duplicate deliveries can't double-fund or double-release
escrow. `status` then tracks what happened to that claim so a genuine
processing failure can be retried by our own background job (Phase 10)
without ever letting Paystack's own redelivery reprocess a claimed-but-not-
yet-completed event.
"""
from datetime import datetime
from typing import Optional
from beanie import Document
from app.core.time import utc_now
from pydantic import Field
from pymongo import IndexModel

WEBHOOK_EVENT_STATUSES = ("processing", "completed", "failed_pending_retry")


class ProcessedWebhookEvent(Document):
    gateway: str
    event_id: str
    event_type: str
    status: str = "completed"  # one of WEBHOOK_EVENT_STATUSES
    payload: Optional[dict] = None  # raw event, needed to retry
    attempts: int = 0
    next_retry_at: Optional[datetime] = None
    last_error: Optional[str] = None
    processed_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "processed_webhook_events"
        indexes = [
            IndexModel([("gateway", 1), ("event_id", 1)], unique=True),
        ]
