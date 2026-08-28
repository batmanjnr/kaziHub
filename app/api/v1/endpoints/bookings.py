from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from bson import ObjectId

from app.api.deps import get_current_user
from app.core.websocket_manager import manager
from app.models.booking import (
    Booking,
    BookingResponse,
    BookingStatus,
    BookingType,
    CustomQuoteRequestCreate,
    FixedBookingCreate,
    GigPurchaseCreate,
    SendQuoteSchema,
)
from app.models.chat import Conversation, Message
from app.models.user import User

router = APIRouter()


def build_booking_response(b: Booking) -> BookingResponse:
    client_id = str(b.client.ref.id if hasattr(b.client, "ref") else b.client.id)
    artisan_id = str(b.artisan.ref.id if hasattr(b.artisan, "ref") else b.artisan.id)
    return BookingResponse(
        id=str(b.id),
        client_id=client_id,
        artisan_id=artisan_id,
        gig_id=b.gig_id,
        booking_type=b.booking_type,
        title=b.title,
        description=b.description,
        attachments=b.attachments,
        amount=b.amount,
        quote_breakdown=b.quote_breakdown,
        address=b.address,
        landmark_hint=b.landmark_hint,
        status=b.status,
        payment_reference=b.payment_reference,
        created_at=b.created_at,
    )


async def get_or_create_conversation(client: User, artisan: User, booking: Booking) -> Conversation:
    conv = await Conversation.find_one({
        "client.$id": client.id,
        "artisan.$id": artisan.id
    })
    if not conv:
        conv = Conversation(client=client, artisan=artisan)
        await conv.insert()

    conv.active_booking_id = str(booking.id)
    conv.active_job_title = booking.title
    conv.active_job_amount = booking.amount
    await conv.save()
    return conv


# ---------------------------------------------------------
# 1. FIXED-PRICE SERVICE BOOKING
# ---------------------------------------------------------
@router.post("/fixed", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def create_fixed_service_booking(
    payload: FixedBookingCreate,
    current_user: User = Depends(get_current_user),
):
    """Client creates fixed-price booking."""
    artisan = await User.get(ObjectId(payload.artisan_id))
    if not artisan or artisan.role != "artisan":
        raise HTTPException(status_code=404, detail="Artisan not found.")

    booking = Booking(
        client=current_user,
        artisan=artisan,
        booking_type=BookingType.FIXED_SERVICE,
        title=payload.service_title,
        amount=payload.amount,
        description=payload.description,
        address=payload.address,
        landmark_hint=payload.landmark_hint,
        attachments=payload.attachments,
        status=BookingStatus.PENDING,
    )
    await booking.insert()
    await get_or_create_conversation(current_user, artisan, booking)
    return build_booking_response(booking)


# ---------------------------------------------------------
# 2. CUSTOM QUOTE REQUEST FLOW
# ---------------------------------------------------------
@router.post("/quote-request", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def request_custom_quote(
    payload: CustomQuoteRequestCreate,
    current_user: User = Depends(get_current_user),
):
    """Client requests custom estimate/inspection."""
    artisan = await User.get(ObjectId(payload.artisan_id))
    if not artisan or artisan.role != "artisan":
        raise HTTPException(status_code=404, detail="Artisan not found.")

    booking = Booking(
        client=current_user,
        artisan=artisan,
        booking_type=BookingType.CUSTOM_QUOTE,
        title=payload.service_title,
        amount=0.0,
        description=payload.description,
        address=payload.address,
        landmark_hint=payload.landmark_hint,
        attachments=payload.attachments,
        status=BookingStatus.QUOTE_REQUESTED,
    )
    await booking.insert()
    conv = await get_or_create_conversation(current_user, artisan, booking)

    # Broadcast system message
    msg = Message(
        conversation_id=str(conv.id),
        sender_id=str(current_user.id),
        content=f"Requested a quote for '{booking.title}'",
        message_type="booking_update",
    )
    await msg.insert()

    await manager.broadcast_to_conversation(str(conv.id), {
        "event": "booking_updated",
        "booking_id": str(booking.id),
        "status": booking.status,
    })

    return build_booking_response(booking)


@router.post("/{booking_id}/quote", response_model=BookingResponse)
async def send_custom_quote(
    booking_id: str,
    payload: SendQuoteSchema,
    current_user: User = Depends(get_current_user),
):
    """Artisan submits custom quote in response to request."""
    booking = await Booking.get(ObjectId(booking_id))
    if not booking or str(booking.artisan.ref.id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    if booking.status != BookingStatus.QUOTE_REQUESTED:
        raise HTTPException(status_code=400, detail="Booking is not awaiting a quote.")

    booking.amount = payload.amount
    booking.quote_breakdown = payload.breakdown
    booking.status = BookingStatus.QUOTE_SENT
    await booking.save()

    conv = await Conversation.find_one({
        "client.$id": booking.client.ref.id,
        "artisan.$id": current_user.id
    })
    if conv:
        conv.active_job_amount = payload.amount
        await conv.save()

        msg = Message(
            conversation_id=str(conv.id),
            sender_id=str(current_user.id),
            content=f"Sent a quote of ₦{payload.amount:,.2f} for '{booking.title}'",
            message_type="quote_offer",
            quote_data={
                "booking_id": str(booking.id),
                "amount": payload.amount,
                "breakdown": payload.breakdown
            }
        )
        await msg.insert()

        # Real-time WebSocket event to trigger live quote card in client's screen
        await manager.broadcast_to_conversation(str(conv.id), {
            "event": "quote_received",
            "message": {
                "id": str(msg.id),
                "conversation_id": msg.conversation_id,
                "sender_id": msg.sender_id,
                "content": msg.content,
                "message_type": msg.message_type,
                "quote_data": msg.quote_data,
                "created_at": msg.created_at.isoformat(),
            }
        })

    return build_booking_response(booking)


@router.post("/{booking_id}/quote/accept", response_model=BookingResponse)
async def accept_custom_quote(
    booking_id: str,
    current_user: User = Depends(get_current_user),
):
    """Client accepts artisan's quote."""
    booking = await Booking.get(ObjectId(booking_id))
    if not booking or str(booking.client.ref.id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    if booking.status != BookingStatus.QUOTE_SENT:
        raise HTTPException(status_code=400, detail="Quote not available for acceptance.")

    booking.status = BookingStatus.ACCEPTED
    await booking.save()

    conv = await Conversation.find_one({
        "client.$id": current_user.id,
        "artisan.$id": booking.artisan.ref.id
    })
    if conv:
        await manager.broadcast_to_conversation(str(conv.id), {
            "event": "quote_accepted",
            "booking_id": str(booking.id),
            "status": booking.status,
        })

    return build_booking_response(booking)


# ---------------------------------------------------------
# 3. DIRECT GIG / ITEM PURCHASE FLOW
# ---------------------------------------------------------
@router.post("/buy-gig", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def buy_gig_item(
    payload: GigPurchaseCreate,
    current_user: User = Depends(get_current_user),
):
    """Client directly purchases a listed item/gig (e.g. Wardrobe)."""
    artisan = await User.get(ObjectId(payload.artisan_id))
    if not artisan:
        raise HTTPException(status_code=404, detail="Artisan not found.")

    booking = Booking(
        client=current_user,
        artisan=artisan,
        gig_id=payload.gig_id,
        booking_type=BookingType.GIG_PURCHASE,
        title=payload.item_title,
        amount=payload.amount,
        address=payload.delivery_address,
        landmark_hint=payload.landmark_hint,
        status=BookingStatus.ACCEPTED,
    )
    await booking.insert()
    await get_or_create_conversation(current_user, artisan, booking)
    return build_booking_response(booking)


# ---------------------------------------------------------
# GENERAL BOOKING MANAGEMENT & STATUS TRANSITIONS
# ---------------------------------------------------------
@router.get("/me", response_model=List[BookingResponse])
async def get_my_bookings(current_user: User = Depends(get_current_user)):
    """Retrieve all bookings for current user."""
    query = {"$or": [{"client.$id": current_user.id}, {"artisan.$id": current_user.id}]}
    bookings = await Booking.find(query).sort("-created_at").to_list()
    return [build_booking_response(b) for b in bookings]


@router.patch("/{booking_id}/status", response_model=BookingResponse)
async def update_booking_status(
    booking_id: str,
    status_update: BookingStatus,
    current_user: User = Depends(get_current_user),
):
    """Transition booking status (in_progress, completed_by_artisan, cancelled)."""
    booking = await Booking.get(ObjectId(booking_id))
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")

    c_id = str(booking.client.ref.id)
    a_id = str(booking.artisan.ref.id)
    u_id = str(current_user.id)

    if u_id not in [c_id, a_id]:
        raise HTTPException(status_code=403, detail="Not authorized.")

    if status_update == BookingStatus.IN_PROGRESS and u_id == a_id:
        if booking.status != BookingStatus.ESCROW_FUNDED:
            raise HTTPException(status_code=400, detail="Escrow must be funded first.")
        booking.status = BookingStatus.IN_PROGRESS

    elif status_update == BookingStatus.COMPLETED_BY_ARTISAN and u_id == a_id:
        if booking.status != BookingStatus.IN_PROGRESS:
            raise HTTPException(status_code=400, detail="Job must be in progress to complete.")
        booking.status = BookingStatus.COMPLETED_BY_ARTISAN

    elif status_update == BookingStatus.CANCELLED:
        if booking.status in [BookingStatus.PAID_OUT, BookingStatus.COMPLETED_BY_ARTISAN]:
            raise HTTPException(status_code=400, detail="Cannot cancel completed job.")
        booking.status = BookingStatus.CANCELLED

    await booking.save()

    conv = await Conversation.find_one({
        "client.$id": booking.client.ref.id,
        "artisan.$id": booking.artisan.ref.id
    })
    if conv:
        await manager.broadcast_to_conversation(str(conv.id), {
            "event": "booking_status_changed",
            "booking_id": str(booking.id),
            "status": booking.status,
        })

    return build_booking_response(booking)