# app/api/v1/endpoints/notifications.py
from typing import List
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import get_current_user
from app.core.config import settings
from app.models.notification import (
    NOTIFICATION_TYPES,
    Notification,
    NotificationPreferences,
    NotificationPreferencesUpdate,
    NotificationResponse,
)
from app.models.push_subscription import (
    PushPublicKeyResponse,
    PushSubscription,
    PushSubscriptionCreate,
    PushSubscriptionDelete,
)
from app.models.user import User
from app.services.push import push_configured

router = APIRouter()


def build_notification_response(n: Notification) -> NotificationResponse:
    booking_id = None
    if n.booking:
        booking_id = str(n.booking.ref.id if hasattr(n.booking, "ref") else n.booking.id)
    return NotificationResponse(
        id=str(n.id),
        type=n.type,
        title=n.title,
        message=n.message,
        booking_id=booking_id,
        is_read=n.is_read,
        channel=n.channel,
        created_at=n.created_at,
    )


@router.get("/", response_model=List[NotificationResponse])
async def list_notifications(
    unread_only: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    query: dict = {"user.$id": current_user.id}
    if unread_only:
        query["is_read"] = False
    notifications = (
        await Notification.find(query)
        .sort("-created_at")
        .skip(offset)
        .limit(limit)
        .to_list()
    )
    return [build_notification_response(n) for n in notifications]


@router.get("/types")
async def list_notification_types():
    """Every value a notification's `type` can take, with what it means (ask 31)."""
    return NOTIFICATION_TYPES


@router.get("/preferences", response_model=NotificationPreferences)
async def get_notification_preferences(current_user: User = Depends(get_current_user)):
    """The user's notification switches (ask 12). `email_summaries`: a daily
    email listing unread notifications. `push_enabled`: every notification
    is also pushed to the devices registered with
    POST /notifications/push/subscriptions. In-app notifications are always
    created."""
    return NotificationPreferences(
        push_enabled=current_user.push_enabled, email_summaries=current_user.email_summaries
    )


@router.put("/preferences", response_model=NotificationPreferences)
async def update_notification_preferences(
    payload: NotificationPreferencesUpdate, current_user: User = Depends(get_current_user)
):
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(current_user, field, value)
    await current_user.save()
    return NotificationPreferences(
        push_enabled=current_user.push_enabled, email_summaries=current_user.email_summaries
    )


@router.get("/push/public-key", response_model=PushPublicKeyResponse)
async def get_push_public_key():
    """Web Push setup (ask 44). In the browser/PWA, after the user allows
    notifications:

        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true, applicationServerKey: <public_key>,
        });
        POST /notifications/push/subscriptions with sub.toJSON()

    Each push carries JSON: {"notification_id", "type", "title", "body",
    "booking_id"} for the service worker's `push` handler to show.
    """
    if not push_configured():
        return PushPublicKeyResponse(enabled=False)
    return PushPublicKeyResponse(enabled=True, public_key=settings.VAPID_PUBLIC_KEY)


@router.post("/push/subscriptions", status_code=status.HTTP_201_CREATED)
async def subscribe_to_push(
    payload: PushSubscriptionCreate, request: Request = None, current_user: User = Depends(get_current_user)
):
    """Register this device for push. Send the browser's
    `PushSubscription.toJSON()` as is. Re-sending the same endpoint (or a
    device changing hands) just updates it."""
    existing = await PushSubscription.find_one({"endpoint": payload.endpoint})
    fields = {
        "p256dh": payload.keys.p256dh,
        "auth": payload.keys.auth,
        "user_agent": request.headers.get("user-agent") if request else None,
    }
    if existing:
        existing.user = current_user
        for k, v in fields.items():
            setattr(existing, k, v)
        await existing.save()
    else:
        await PushSubscription(user=current_user, endpoint=payload.endpoint, **fields).insert()
    return {"detail": "Push notifications enabled on this device."}


@router.delete("/push/subscriptions", status_code=status.HTTP_200_OK)
async def unsubscribe_from_push(
    payload: PushSubscriptionDelete, current_user: User = Depends(get_current_user)
):
    """Stop pushing to this device (call alongside the browser's
    `subscription.unsubscribe()`, and on sign-out)."""
    await PushSubscription.find({"endpoint": payload.endpoint, "user.$id": current_user.id}).delete()
    return {"detail": "Push notifications disabled on this device."}


@router.patch("/read-all", status_code=status.HTTP_200_OK)
async def mark_all_notifications_read(current_user: User = Depends(get_current_user)):
    await Notification.find({"user.$id": current_user.id, "is_read": False}).update(
        {"$set": {"is_read": True}}
    )
    return {"detail": "All notifications marked as read."}


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(
    notification_id: str, current_user: User = Depends(get_current_user)
):
    try:
        notification = await Notification.get(PydanticObjectId(notification_id))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid notification ID.")
    if not notification:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Notification not found.")

    owner_id = (
        notification.user.ref.id if hasattr(notification.user, "ref") else notification.user.id
    )
    if str(owner_id) != str(current_user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not authorized.")

    notification.is_read = True
    await notification.save()
    return build_notification_response(notification)
