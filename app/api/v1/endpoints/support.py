# app/api/v1/endpoints/support.py
"""Help & Support requests (frontend ask 14)."""
import secrets
from typing import List

from beanie import PydanticObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.core.config import settings
from app.models.booking import Booking
from app.models.support_ticket import SupportTicket, SupportTicketCreate, SupportTicketResponse
from app.models.user import User
from app.services.email import send_support_ticket_email
from app.services.notifications import notify

router = APIRouter()


def build_ticket_response(t: SupportTicket) -> SupportTicketResponse:
    return SupportTicketResponse(
        id=str(t.id),
        ticket_number=t.ticket_number,
        subject=t.subject,
        message=t.message,
        booking_id=t.booking_id,
        status=t.status,
        created_at=t.created_at,
    )


@router.post("/tickets", response_model=SupportTicketResponse, status_code=status.HTTP_201_CREATED)
async def create_support_ticket(
    payload: SupportTicketCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """Send a support request. Returns a real `ticket_number` (e.g.
    "SUP-7F3A9C") to show the user. `booking_id` is optional and must be
    one of the user's own bookings."""
    if payload.booking_id:
        try:
            booking = await Booking.get(PydanticObjectId(payload.booking_id))
        except Exception:
            booking = None
        party_ids = set()
        if booking:
            for link in (booking.client, booking.artisan):
                party_ids.add(link.ref.id if hasattr(link, "ref") else link.id)
        if not booking or current_user.id not in party_ids:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Booking not found.")

    ticket = SupportTicket(
        ticket_number=f"SUP-{secrets.token_hex(3).upper()}",
        user=current_user,
        subject=payload.subject.strip(),
        message=payload.message.strip(),
        booking_id=payload.booking_id,
    )
    await ticket.insert()

    await notify(
        current_user, "support_ticket_update", "Support request received",
        f"We've received your request {ticket.ticket_number} and will reply by email.",
    )
    if settings.SUPPORT_EMAIL:
        background_tasks.add_task(
            send_support_ticket_email,
            settings.SUPPORT_EMAIL,
            ticket.ticket_number,
            f"{current_user.first_name} {current_user.last_name} <{current_user.email}>",
            ticket.subject,
            ticket.message,
            ticket.booking_id,
        )
    return build_ticket_response(ticket)


@router.get("/tickets", response_model=List[SupportTicketResponse])
async def list_my_support_tickets(current_user: User = Depends(get_current_user)):
    """The user's own support requests, newest first."""
    tickets = await SupportTicket.find({"user.$id": current_user.id}).sort("-created_at").to_list()
    return [build_ticket_response(t) for t in tickets]
