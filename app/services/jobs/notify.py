# app/services/jobs/notify.py
"""Dispatches pending SMS/WhatsApp notifications (spec §7.3), recording the
outcome on notifications.delivery_status so a silent send failure is
visible (spec §4.13's whole reason for existing)."""
from app.models.notification import Notification
from app.models.user import User
from app.services.sms import send_sms, send_whatsapp


async def dispatch_pending_notifications() -> dict:
    pending = await Notification.find(
        {"channel": {"$in": ["sms", "whatsapp"]}, "delivery_status": "pending"}
    ).to_list()

    sent, failed = 0, 0
    for notification in pending:
        user_id = (
            notification.user.ref.id if hasattr(notification.user, "ref") else notification.user.id
        )
        user = await User.get(user_id)
        if not user:
            notification.delivery_status = "failed"
            await notification.save()
            failed += 1
            continue

        try:
            if notification.channel == "sms":
                ok = await send_sms(user.phone_number, notification.message)
            else:
                ok = await send_whatsapp(user.phone_number, notification.message)
        except Exception:
            ok = False

        notification.delivery_status = "sent" if ok else "failed"
        await notification.save()
        if ok:
            sent += 1
        else:
            failed += 1

    return {"total": len(pending), "sent": sent, "failed": failed}
