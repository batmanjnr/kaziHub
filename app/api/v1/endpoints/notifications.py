# app/api/v1/endpoints/notifications.py
from typing import List
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_user
from app.models.notification import Notification, NotificationResponse
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
