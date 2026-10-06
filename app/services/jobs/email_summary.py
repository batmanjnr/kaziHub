# app/services/jobs/email_summary.py
"""Daily email summary of unread notifications (frontend ask 12), sent at
07:00 Lagos time to users with `email_summaries` on. Each notification is
summarised at most once (`emailed_in_summary`)."""
import asyncio
from collections import defaultdict

from app.models.notification import Notification
from app.models.user import User
from app.services.email import send_notification_summary_email

MAX_LINES = 10


async def send_daily_email_summaries() -> dict:
    pending = await Notification.find(
        {"is_read": False, "emailed_in_summary": {"$ne": True}}
    ).sort("-created_at").to_list()

    by_user = defaultdict(list)
    for n in pending:
        by_user[n.user.ref.id if hasattr(n.user, "ref") else n.user.id].append(n)

    sent = skipped = 0
    for user_id, notes in by_user.items():
        user = await User.get(user_id)
        if not user or not user.email_summaries or user.deleted_at is not None or not user.is_active:
            skipped += 1
            continue
        lines = [f"{n.title}: {n.message}" for n in notes[:MAX_LINES]]
        if len(notes) > MAX_LINES:
            lines.append(f"...and {len(notes) - MAX_LINES} more")
        ok = await asyncio.to_thread(send_notification_summary_email, user.email, user.first_name, lines)
        if ok:
            await Notification.find({"_id": {"$in": [n.id for n in notes]}}).update(
                {"$set": {"emailed_in_summary": True}}
            )
            sent += 1
    return {"users_emailed": sent, "users_skipped": skipped}
