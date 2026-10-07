# app/services/push.py
"""Web Push delivery (frontend ask 44).

Every notification created through app.services.notifications.notify is
also pushed to the user's subscribed devices when their `push_enabled`
preference is on and the server has VAPID keys. Sending happens in the
background (pywebpush is a blocking HTTP client, so it runs in a worker
thread) and never fails the action that caused the notification. A device
whose subscription has expired (404/410 from the push service) is dropped.
"""
import asyncio
import json
import logging
from typing import Optional, Set

from pywebpush import WebPushException, webpush

from app.core.config import settings
from app.models.push_subscription import PushSubscription
from app.models.user import User

logger = logging.getLogger("kazihub.push")

# Keep references to in-flight sends so they aren't garbage-collected.
_pending: Set[asyncio.Task] = set()


def push_configured() -> bool:
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def _send_one(sub: PushSubscription, payload: str) -> Optional[int]:
    """Returns an HTTP status if the push service rejected the
    subscription, else None."""
    try:
        webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            data=payload,
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.VAPID_CLAIMS_EMAIL},
            ttl=24 * 60 * 60,
            timeout=10,
        )
        return None
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status not in (404, 410):
            logger.warning("Push to %s failed: %s", sub.endpoint[:60], e)
        return status
    except Exception:
        logger.exception("Push to %s failed", sub.endpoint[:60])
        return None


async def _deliver(user: User, payload: dict) -> None:
    subs = await PushSubscription.find({"user.$id": user.id}).to_list()
    body = json.dumps(payload)
    for sub in subs:
        status = await asyncio.to_thread(_send_one, sub, body)
        if status in (404, 410):
            await sub.delete()


def push_to_user(user: User, payload: dict) -> None:
    """Fire-and-forget push of `payload` to all of `user`'s devices."""
    if not push_configured() or not user.push_enabled:
        return
    task = asyncio.create_task(_deliver(user, payload))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
