# app/services/jobs/webhook_retry.py
"""Retries webhook events wallet.py's handler failed to process (spec §7.4).

A webhook whose handler raised after the signature check and the
processed_webhook_events claim is left in `failed_pending_retry` rather
than propagating a 500 back to Paystack — see wallet.py's docstring for why
relying on Paystack's own redelivery doesn't work once the claim exists.
This job is what actually completes it, with exponential backoff, checking
processed_webhook_events (implicitly — it *is* the record) to stay
idempotent across retries.
"""
from datetime import datetime, timedelta, timezone

from app.api.v1.endpoints.wallet import dispatch_webhook_event
from app.models.webhook_event import ProcessedWebhookEvent

MAX_ATTEMPTS = 6


def _backoff_minutes(attempts: int) -> int:
    return min(2**attempts, 60)


async def retry_failed_webhooks() -> dict:
    now = datetime.now(timezone.utc)
    due = await ProcessedWebhookEvent.find(
        {"status": "failed_pending_retry", "next_retry_at": {"$lte": now}}
    ).to_list()

    retried, still_failing, exhausted = 0, 0, 0
    for record in due:
        if record.attempts >= MAX_ATTEMPTS:
            exhausted += 1
            continue

        payload = record.payload or {}
        data = payload.get("data", {})
        try:
            await dispatch_webhook_event(record.event_type, data)
            record.status = "completed"
            record.last_error = None
            record.next_retry_at = None
            await record.save()
            retried += 1
        except Exception as e:
            record.attempts += 1
            record.last_error = str(e)
            record.next_retry_at = now + timedelta(minutes=_backoff_minutes(record.attempts))
            await record.save()
            still_failing += 1

    return {
        "due": len(due),
        "completed": retried,
        "still_failing": still_failing,
        "exhausted": exhausted,
    }
