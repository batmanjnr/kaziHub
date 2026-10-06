# app/api/v1/endpoints/notifications.py
from typing import List
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_user
from app.models.notification import (
    NOTIFICATION_TYPES,
    Notification,
    NotificationPreferences,
    NotificationPreferencesUpdate,
    NotificationResponse,
)
from app.models.user import User

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
    email listing unread notifications. `push_enabled`: stored for when push
    delivery is added; in-app notifications are always created."""
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
