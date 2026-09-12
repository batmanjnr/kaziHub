# app/api/v1/endpoints/bookings.py
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_user
from app.core.idempotency import cache_response, get_cached_response, require_idempotency_key
from app.core.websocket_manager import manager
from app.models.booking import (
    Booking,
    BookingDetailResponse,
    BookingResponse,
    BookingStatus,
    BookingStatusHistoryEntry,
    BookingType,
    CustomQuoteRequestCreate,
    EscrowStatus,
    FixedBookingCreate,
    GigPurchaseCreate,
    SendQuoteSchema,
)
from app.models.booking_status_history import BookingStatusHistory
from app.models.chat import Conversation, Message
from app.models.dispute import Dispute, DisputeCreate, DisputeResponse
from app.models.gig import Gig
from app.models.profile import Profile
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.services.payouts import request_artisan_payout
from app.services.booking_transitions import apply_booking_transition

router = APIRouter()

AUTO_RELEASE_WINDOW = timedelta(days=4)
DEFAULT_COMMISSION_RATE = 0.10


def generate_reference_code() -> str:
    return f"KZ-{secrets.token_hex(5).upper()}"


def compute_escrow_breakdown(amount: float, commission_rate: float = DEFAULT_COMMISSION_RATE) -> dict:
    platform_fee = round(amount * commission_rate, 2)
    return {
        "escrow_amount": amount,
        "platform_fee": platform_fee,
        "platform_commission_rate": commission_rate,
        "artisan_earnings": round(amount - platform_fee, 2),
    }


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
        escrow_status=b.escrow_status,
        payment_reference=b.payment_reference,
        reference_code=b.reference_code,
        escrow_amount=b.escrow_amount,
        platform_fee=b.platform_fee,
        gateway_fee=b.gateway_fee,
        artisan_earnings=b.artisan_earnings,
        platform_commission_rate=b.platform_commission_rate,
        scheduled_date=b.scheduled_date,
        completion_description=b.completion_description,
        completion_photos=b.completion_photos,
        auto_completion_deadline=b.auto_completion_deadline,
        lock_version=b.lock_version,
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


async def _load_booking_or_404(booking_id: str) -> Booking:
    try:
        booking = await Booking.get(ObjectId(booking_id))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid booking ID.")
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Booking not found.")
    return booking


def _participant_ids(booking: Booking) -> tuple:
    c_id = str(booking.client.ref.id if hasattr(booking.client, "ref") else booking.client.id)
    a_id = str(booking.artisan.ref.id if hasattr(booking.artisan, "ref") else booking.artisan.id)
    return c_id, a_id


# ---------------------------------------------------------
# 1. FIXED-PRICE SERVICE BOOKING
# ---------------------------------------------------------
@router.post("/fixed", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def create_fixed_service_booking(
    payload: FixedBookingCreate,
    current_user: User = Depends(get_current_user),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Client creates fixed-price booking. Requires Idempotency-Key."""
    cached = await get_cached_response(idempotency_key, current_user, "bookings.create_fixed")
    if cached:
        return cached

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
        reference_code=generate_reference_code(),
        **compute_escrow_breakdown(payload.amount),
    )
    await booking.insert()
    await BookingStatusHistory(
        booking=booking, from_status=None, to_status=booking.status.value, changed_by=current_user
    ).insert()
    await get_or_create_conversation(current_user, artisan, booking)

    response = build_booking_response(booking)
    await cache_response(
        idempotency_key, current_user, "bookings.create_fixed", response.model_dump(mode="json"), 201
    )
    return response


# ---------------------------------------------------------
# 2. CUSTOM QUOTE REQUEST FLOW
# ---------------------------------------------------------
@router.post("/quote-request", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def request_custom_quote(
    payload: CustomQuoteRequestCreate,
    current_user: User = Depends(get_current_user),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Client requests custom estimate/inspection. Requires Idempotency-Key."""
    cached = await get_cached_response(idempotency_key, current_user, "bookings.quote_request")
    if cached:
        return cached

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
        reference_code=generate_reference_code(),
    )
    await booking.insert()
    await BookingStatusHistory(
        booking=booking, from_status=None, to_status=booking.status.value, changed_by=current_user
    ).insert()
    conv = await get_or_create_conversation(current_user, artisan, booking)

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

    response = build_booking_response(booking)
    await cache_response(
        idempotency_key, current_user, "bookings.quote_request", response.model_dump(mode="json"), 201
    )
    return response


@router.post("/{booking_id}/quote", response_model=BookingResponse)
async def send_custom_quote(
    booking_id: str,
    payload: SendQuoteSchema,
    current_user: User = Depends(get_current_user),
):
    """Artisan submits custom quote in response to request."""
    booking = await _load_booking_or_404(booking_id)
    _, artisan_id = _participant_ids(booking)
    if artisan_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    booking = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.QUOTE_REQUESTED],
        updates={
            "amount": payload.amount,
            "quote_breakdown": payload.breakdown,
            **compute_escrow_breakdown(payload.amount),
        },
        to_status=BookingStatus.QUOTE_SENT,
        changed_by=current_user,
    )

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
    booking = await _load_booking_or_404(booking_id)
    client_id, _ = _participant_ids(booking)
    if client_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    booking = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.QUOTE_SENT],
        to_status=BookingStatus.ACCEPTED,
        changed_by=current_user,
    )

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
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Client directly purchases a listed item/gig. Requires Idempotency-Key."""
    cached = await get_cached_response(idempotency_key, current_user, "bookings.buy_gig")
    if cached:
        return cached

    artisan = await User.get(ObjectId(payload.artisan_id))
    if not artisan:
        raise HTTPException(status_code=404, detail="Artisan not found.")

    # FIX (security audit): the booking's amount/escrow used to come
    # straight from the client-supplied payload.amount with no cross-check
    # against the gig's actual listed price — a client could buy any gig
    # for an arbitrary (e.g. near-zero) amount. The gig's own price is now
    # the sole source of truth for what gets charged.
    try:
        gig = await Gig.get(ObjectId(payload.gig_id))
    except Exception:
        gig = None
    if not gig or not gig.is_active:
        raise HTTPException(status_code=404, detail="Gig not found or no longer available.")

    gig_profile_id = gig.artisan_profile.ref.id if hasattr(gig.artisan_profile, "ref") else gig.artisan_profile.id
    gig_profile = await Profile.get(gig_profile_id)
    if not gig_profile:
        raise HTTPException(status_code=404, detail="Gig owner's profile not found.")
    gig_artisan_id = gig_profile.user.ref.id if hasattr(gig_profile.user, "ref") else gig_profile.user.id
    if str(gig_artisan_id) != str(artisan.id):
        raise HTTPException(status_code=400, detail="This gig does not belong to the specified artisan.")

    booking = Booking(
        client=current_user,
        artisan=artisan,
        gig_id=payload.gig_id,
        booking_type=BookingType.GIG_PURCHASE,
        title=payload.item_title,
        amount=gig.price,
        address=payload.delivery_address,
        landmark_hint=payload.landmark_hint,
        status=BookingStatus.ACCEPTED,
        reference_code=generate_reference_code(),
        **compute_escrow_breakdown(gig.price),
    )
    await booking.insert()
    await BookingStatusHistory(
        booking=booking, from_status=None, to_status=booking.status.value, changed_by=current_user
    ).insert()
    await get_or_create_conversation(current_user, artisan, booking)

    # NOTE (security audit): gig.orders_count is incremented when escrow is
    # actually funded (wallet.py's _handle_charge_success), not here —
    # incrementing it at mere booking creation let anyone manufacture fake
    # "sold" counts for a gig without ever paying.

    response = build_booking_response(booking)
    await cache_response(
        idempotency_key, current_user, "bookings.buy_gig", response.model_dump(mode="json"), 201
    )
    return response


# ---------------------------------------------------------
# ARTISAN RESPONSE TO FIXED-PRICE / GIG-PURCHASE BOOKINGS
# ---------------------------------------------------------
@router.post("/{booking_id}/accept", response_model=BookingResponse)
async def accept_booking(booking_id: str, current_user: User = Depends(get_current_user)):
    """Artisan accepts a pending fixed-price or gig-purchase booking."""
    booking = await _load_booking_or_404(booking_id)
    _, artisan_id = _participant_ids(booking)
    if artisan_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    booking = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.PENDING],
        to_status=BookingStatus.ACCEPTED,
        changed_by=current_user,
    )
    return build_booking_response(booking)


@router.post("/{booking_id}/decline", response_model=BookingResponse)
async def decline_booking(booking_id: str, current_user: User = Depends(get_current_user)):
    """Artisan declines a pending fixed-price or gig-purchase booking."""
    booking = await _load_booking_or_404(booking_id)
    _, artisan_id = _participant_ids(booking)
    if artisan_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    booking = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.PENDING],
        updates={"cancellation_reason": "Declined by artisan", "cancelled_at": datetime.now(timezone.utc)},
        to_status=BookingStatus.CANCELLED,
        changed_by=current_user,
        reason="Declined by artisan",
    )
    return build_booking_response(booking)


# ---------------------------------------------------------
# ESCROW FUNDING CONFIRMATION
# ---------------------------------------------------------
@router.post("/{booking_id}/fund-escrow", response_model=BookingResponse)
async def confirm_fund_escrow(
    booking_id: str,
    current_user: User = Depends(get_current_user),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Confirms escrow payment after the Paystack gateway callback, in
    addition to the webhook (spec §10) — covers the case where the webhook
    is delayed relative to the client's redirect back into the app.
    """
    cached = await get_cached_response(idempotency_key, current_user, "bookings.fund_escrow")
    if cached:
        return cached

    booking = await _load_booking_or_404(booking_id)
    client_id, _ = _participant_ids(booking)
    if client_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    tx = await Transaction.find_one({
        "booking.$id": booking.id,
        "type": TransactionType.ESCROW_DEPOSIT.value,
        "status": TransactionStatus.SUCCESSFUL.value,
    })
    if not tx:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Escrow payment has not yet been confirmed by the payment gateway.",
        )

    booking = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.ACCEPTED, BookingStatus.PENDING],
        allowed_escrow_statuses=[EscrowStatus.UNFUNDED],
        updates={
            "escrow_status": EscrowStatus.HELD_IN_ESCROW,
            "escrow_funded_at": datetime.now(timezone.utc),
            "payment_reference": tx.transaction_reference,
        },
        to_status=BookingStatus.ESCROW_FUNDED,
        changed_by=current_user,
    )

    conv = await Conversation.find_one({
        "client.$id": booking.client.ref.id,
        "artisan.$id": booking.artisan.ref.id,
    })
    if conv:
        await manager.broadcast_to_conversation(str(conv.id), {
            "event": "escrow_funded",
            "booking_id": str(booking.id),
            "status": booking.status,
        })

    response = build_booking_response(booking)
    await cache_response(
        idempotency_key, current_user, "bookings.fund_escrow", response.model_dump(mode="json"), 200
    )
    return response


# ---------------------------------------------------------
# GENERAL BOOKING MANAGEMENT & STATUS TRANSITIONS
# ---------------------------------------------------------
@router.get("/me", response_model=List[BookingResponse])
async def get_my_bookings(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    # FIX (security review): previously unbounded — a long-lived account
    # with thousands of bookings could force a very large response.
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """Retrieve all bookings for current user, optionally filtered by status."""
    query: dict = {"$or": [{"client.$id": current_user.id}, {"artisan.$id": current_user.id}]}
    if status_filter:
        query["status"] = status_filter
    bookings = (
        await Booking.find(query).sort("-created_at").skip(offset).limit(limit).to_list()
    )
    return [build_booking_response(b) for b in bookings]


@router.get("/{booking_id}", response_model=BookingDetailResponse)
async def get_booking_detail(booking_id: str, current_user: User = Depends(get_current_user)):
    """Full booking details, payment summary, and a real timeline sourced
    from booking_status_history."""
    booking = await _load_booking_or_404(booking_id)
    c_id, a_id = _participant_ids(booking)
    if str(current_user.id) not in (c_id, a_id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    history = (
        await BookingStatusHistory.find({"booking.$id": booking.id})
        .sort("created_at")
        .to_list()
    )
    timeline = [
        BookingStatusHistoryEntry(
            from_status=h.from_status,
            to_status=h.to_status,
            changed_by=str(h.changed_by.ref.id) if h.changed_by and hasattr(h.changed_by, "ref") else None,
            reason=h.reason,
            created_at=h.created_at,
        )
        for h in history
    ]

    base = build_booking_response(booking)
    return BookingDetailResponse(**base.model_dump(), timeline=timeline)


@router.post("/{booking_id}/submit-completion", response_model=BookingResponse)
async def submit_completion(
    booking_id: str,
    current_user: User = Depends(get_current_user),
):
    """Artisan submits proof of completion, starting the 4-day auto-release
    window (spec §6/§7)."""
    booking = await _load_booking_or_404(booking_id)
    _, artisan_id = _participant_ids(booking)
    if artisan_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    now = datetime.now(timezone.utc)
    booking = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.IN_PROGRESS],
        allowed_escrow_statuses=[EscrowStatus.HELD_IN_ESCROW],
        updates={
            "completion_submitted_at": now,
            "auto_completion_deadline": now + AUTO_RELEASE_WINDOW,
        },
        to_status=BookingStatus.COMPLETED_BY_ARTISAN,
        changed_by=current_user,
    )
    return build_booking_response(booking)


async def core_confirm_completion(booking: Booking, current_user: User) -> Booking:
    """Shared release logic used by both the spec-named endpoint below and
    wallet.py's legacy /release-escrow alias, so there's exactly one place
    that moves a booking's escrow to released_to_artisan.

    Triggers a real Paystack Transfer to the artisan's verified bank account
    (spec §11.4) before touching the booking at all — if Paystack won't even
    accept the transfer request (e.g. no verified bank account on file), we
    fail loudly here rather than marking the booking paid out with no money
    behind it. The transfer's *final* settlement is confirmed later by the
    transfer.success/failed/reversed webhook (wallet.py), which is why the
    ledger entry below is recorded as PENDING, not SUCCESSFUL.
    """
    artisan_id = booking.artisan.ref.id if hasattr(booking.artisan, "ref") else booking.artisan.id
    payout_amount = booking.artisan_earnings or booking.amount
    payout_result = await request_artisan_payout(
        artisan_id,
        payout_amount,
        reason=f"KaziHub booking {booking.reference_code or booking.id} payout",
    )
    if payout_result["status"] != "pending":
        error = payout_result["gateway_response"].get("error", "Payout could not be initiated.")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Payout failed: {error}")

    async def _release_escrow_writes(booking: Booking, session) -> None:
        tx = Transaction(
            booking=booking,
            transaction_reference=payout_result["reference"],
            amount=payout_amount,
            type=TransactionType.ESCROW_RELEASE,
            status=TransactionStatus.PENDING,
            gateway_response=payout_result["gateway_response"],
        )
        await tx.insert(session=session)

    booking = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.COMPLETED_BY_ARTISAN],
        allowed_escrow_statuses=[EscrowStatus.HELD_IN_ESCROW],
        updates={
            "escrow_status": EscrowStatus.RELEASED_TO_ARTISAN,
            "escrow_released_at": datetime.now(timezone.utc),
        },
        to_status=BookingStatus.PAID_OUT,
        changed_by=current_user,
        extra_writes=_release_escrow_writes,
    )

    try:
        profile = await Profile.find_one({"user.$id": booking.artisan.ref.id})
        if profile:
            profile.completed_jobs_count += 1
            await profile.save()
    except Exception:
        pass

    conv = await Conversation.find_one({
        "client.$id": booking.client.ref.id,
        "artisan.$id": booking.artisan.ref.id,
    })
    if conv:
        await manager.broadcast_to_conversation(str(conv.id), {
            "event": "escrow_released",
            "booking_id": str(booking.id),
            "status": booking.status,
        })

    return booking


@router.post("/{booking_id}/confirm-completion", response_model=BookingResponse)
async def confirm_completion(
    booking_id: str,
    current_user: User = Depends(get_current_user),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Client confirms job completion and releases escrow. Requires
    Idempotency-Key. Acquires the same optimistic lock as the auto-release
    cron and /dispute so exactly one of them wins a race on this booking."""
    cached = await get_cached_response(idempotency_key, current_user, "bookings.confirm_completion")
    if cached:
        return cached

    booking = await _load_booking_or_404(booking_id)
    client_id, _ = _participant_ids(booking)
    if client_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    booking = await core_confirm_completion(booking, current_user)

    response = build_booking_response(booking)
    await cache_response(
        idempotency_key, current_user, "bookings.confirm_completion", response.model_dump(mode="json"), 200
    )
    return response


async def _dispute_writes_factory(client: User, artisan_id, dispute_in: DisputeCreate):
    async def _writes(booking: Booking, session) -> None:
        dispute = Dispute(
            ticket_id=f"DSP-{secrets.token_hex(4).upper()}",
            booking=booking,
            client=client,
            artisan=await User.get(artisan_id),
            reason=dispute_in.reason,
            details=dispute_in.details,
            evidence_photos=dispute_in.evidence_photos,
        )
        await dispute.insert(session=session)

    return _writes


@router.post("/{booking_id}/dispute", response_model=DisputeResponse)
async def dispute_booking(
    booking_id: str,
    payload: DisputeCreate,
    current_user: User = Depends(get_current_user),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Client disputes a booking. Requires Idempotency-Key. Acquires the
    same lock as confirm-completion and the auto-release cron, so a dispute
    filed the same instant the cron fires cannot lose the race silently."""
    cached = await get_cached_response(idempotency_key, current_user, "bookings.dispute")
    if cached:
        return cached

    booking = await _load_booking_or_404(booking_id)
    client_id, artisan_id = _participant_ids(booking)
    if client_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    booking = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED_BY_ARTISAN],
        to_status=BookingStatus.DISPUTED,
        changed_by=current_user,
        reason=payload.reason,
        extra_writes=await _dispute_writes_factory(current_user, booking.artisan.ref.id, payload),
    )

    dispute = await Dispute.find_one({"booking.$id": booking.id})

    response = DisputeResponse(
        id=str(dispute.id),
        ticket_id=dispute.ticket_id,
        booking_id=str(booking.id),
        client_id=client_id,
        artisan_id=artisan_id,
        reason=dispute.reason,
        details=dispute.details,
        evidence_photos=dispute.evidence_photos,
        status=dispute.status,
        resolution_notes=dispute.resolution_notes,
        resolved_at=dispute.resolved_at,
        created_at=dispute.created_at,
    )
    await cache_response(
        idempotency_key, current_user, "bookings.dispute", response.model_dump(mode="json"), 201
    )
    return response


async def _cancel_refund_writes(booking: Booking, session) -> None:
    if booking.escrow_status != EscrowStatus.HELD_IN_ESCROW:
        return
    tx = Transaction(
        booking=booking,
        transaction_reference=f"KZ-REFUND-{str(booking.id)[:8]}-{ObjectId()}",
        amount=booking.escrow_amount or booking.amount,
        type=TransactionType.REFUND,
        status=TransactionStatus.SUCCESSFUL,
    )
    await tx.insert(session=session)


@router.post("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(
    booking_id: str,
    current_user: User = Depends(get_current_user),
):
    """Cancel a booking. If escrow was already funded, records a refund in
    the ledger (the actual Paystack refund call is Phase 9 work)."""
    booking = await _load_booking_or_404(booking_id)
    c_id, a_id = _participant_ids(booking)
    if str(current_user.id) not in (c_id, a_id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    if booking.status in (
        BookingStatus.PAID_OUT,
        BookingStatus.COMPLETED_BY_ARTISAN,
        BookingStatus.DISPUTED,
    ):
        # FIX (security audit): DISPUTED was missing here — a client could
        # file a dispute and then immediately self-cancel to force their
        # own full refund before an admin ever adjudicated it, completely
        # bypassing resolve_dispute. Only an admin resolution can move a
        # disputed booking out of that state now.
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel a completed or disputed job. Disputed bookings must be resolved by an admin.",
        )

    updates = {
        "cancellation_reason": "Cancelled by user",
        "cancelled_at": datetime.now(timezone.utc),
    }
    if booking.escrow_status == EscrowStatus.HELD_IN_ESCROW:
        updates["escrow_status"] = EscrowStatus.REFUNDED_TO_CLIENT
        updates["escrow_released_at"] = datetime.now(timezone.utc)

    booking = await apply_booking_transition(
        booking.id,
        updates=updates,
        to_status=BookingStatus.CANCELLED,
        changed_by=current_user,
        extra_writes=_cancel_refund_writes,
    )

    conv = await Conversation.find_one({
        "client.$id": booking.client.ref.id,
        "artisan.$id": booking.artisan.ref.id,
    })
    if conv:
        await manager.broadcast_to_conversation(str(conv.id), {
            "event": "booking_status_changed",
            "booking_id": str(booking.id),
            "status": booking.status,
        })

    return build_booking_response(booking)


@router.patch("/{booking_id}/status", response_model=BookingResponse)
async def update_booking_status(
    booking_id: str,
    status_update: BookingStatus,
    current_user: User = Depends(get_current_user),
):
    """Legacy generic status transition, kept for existing clients. Prefer
    the named endpoints (accept/decline/submit-completion/confirm-completion)
    for new integrations."""
    booking = await _load_booking_or_404(booking_id)
    c_id, a_id = _participant_ids(booking)
    u_id = str(current_user.id)

    if u_id not in (c_id, a_id):
        raise HTTPException(status_code=403, detail="Not authorized.")

    if status_update == BookingStatus.IN_PROGRESS and u_id == a_id:
        booking = await apply_booking_transition(
            booking.id,
            allowed_statuses=[BookingStatus.ESCROW_FUNDED],
            to_status=BookingStatus.IN_PROGRESS,
            changed_by=current_user,
        )
    elif status_update == BookingStatus.COMPLETED_BY_ARTISAN and u_id == a_id:
        now = datetime.now(timezone.utc)
        booking = await apply_booking_transition(
            booking.id,
            allowed_statuses=[BookingStatus.IN_PROGRESS],
            updates={
                "completion_submitted_at": now,
                "auto_completion_deadline": now + AUTO_RELEASE_WINDOW,
            },
            to_status=BookingStatus.COMPLETED_BY_ARTISAN,
            changed_by=current_user,
        )
    elif status_update == BookingStatus.CANCELLED:
        if booking.status in (
            BookingStatus.PAID_OUT,
            BookingStatus.COMPLETED_BY_ARTISAN,
            BookingStatus.DISPUTED,
        ):
            raise HTTPException(
                status_code=400,
                detail="Cannot cancel a completed or disputed job. Disputed bookings must be resolved by an admin.",
            )
        booking = await apply_booking_transition(
            booking.id, to_status=BookingStatus.CANCELLED, changed_by=current_user
        )
    else:
        raise HTTPException(status_code=400, detail="Unsupported or unauthorized status transition.")

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
