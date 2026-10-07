# app/services/notifications.py
"""In-app notifications for booking and chat events (frontend ask 31).

`notify` is the one way the app creates a notification, so every event
gets the same shape: a documented `type` (app.models.notification.
NOTIFICATION_TYPES) and `booking_id` set whenever there is a booking. It
never raises: a failed notification must not fail the booking action that
triggered it.
"""
import logging
from typing import Optional, Union

from beanie import PydanticObjectId

from app.models.booking import Booking
from app.models.notification import NOTIFICATION_TYPES, Notification
from app.models.user import User
from app.services.push import push_to_user

logger = logging.getLogger("kazihub.notifications")


def link_id(link) -> Optional[PydanticObjectId]:
    if link is None:
        return None
    return link.ref.id if hasattr(link, "ref") else link.id


async def notify(
    user: Union[User, PydanticObjectId, str, None],
    type: str,
    title: str,
    message: str,
    booking: Optional[Booking] = None,
) -> None:
    assert type in NOTIFICATION_TYPES, type
    try:
        if user is None:
            return
        if not isinstance(user, User):
            user = await User.get(PydanticObjectId(str(user)))
            if not user:
                return
        notification = Notification(user=user, type=type, title=title, message=message, booking=booking)
        await notification.insert()
        push_to_user(user, {
            "notification_id": str(notification.id),
            "type": type,
            "title": title,
            "body": message,
            "booking_id": str(booking.id) if booking else None,
        })
    except Exception:
        logger.exception("Failed to create %s notification", type)


async def notify_booking_party(booking: Booking, who: str, type: str, title: str, message: str) -> None:
    """`who` is "client" or "artisan"."""
    target = booking.client if who == "client" else booking.artisan
    await notify(link_id(target), type, title, message, booking=booking)


def naira(amount: float) -> str:
    return f"₦{amount:,.0f}" if float(amount).is_integer() else f"₦{amount:,.2f}"
