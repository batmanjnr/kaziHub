import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.api.deps import get_current_user
from app.api.v1.endpoints.bookings import core_confirm_completion, notify_escrow_funded
from app.core.config import settings
from app.core.websocket_manager import manager
from app.models.audit_log import AuditLog
from app.models.booking import Booking, BookingStatus, BookingType, EscrowStatus
from app.models.chat import Conversation
from app.models.gig import Gig
from app.models.notification import Notification
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.models.webhook_event import ProcessedWebhookEvent
from app.services.booking_transitions import apply_booking_transition
from app.services.paystack import PaystackError, initialize_transaction

router = APIRouter()


class EscrowCheckoutResponse(BaseModel):
    authorization_url: str = Field(description="Send the client here to pay (Paystack checkout).")
    access_code: str = Field(description="For Paystack's inline/popup JS instead of a redirect.")
    reference: str = Field(description="This payment's reference; Paystack adds it to the callback URL.")
    callback_url: Optional[str] = Field(
        default=None,
        description="Where Paystack sends the client after paying: the PAYSTACK_CALLBACK_URL setting plus "
        "`?booking_id=...`; Paystack appends `&reference=...&trxref=...`.",
    )


@router.post("/initialize-escrow/{booking_id}", response_model=EscrowCheckoutResponse)
async def initialize_escrow_payment(
    booking_id: str,
    current_user: User = Depends(get_current_user),
):
    """Start paying a booking into escrow (ask 25).

    1. Call this when the booking is `pending` or `accepted` and unfunded.
    2. Redirect the client to `authorization_url`.
    3. Paystack sends them back to `callback_url`. That page calls
       POST /bookings/{booking_id}/fund-escrow (with an Idempotency-Key),
       which confirms the payment with Paystack if the webhook hasn't
       already, and returns the booking as `escrow_funded`.

    The webhook (POST /wallet/webhook/paystack) also funds the booking on
    its own, so the client closing the tab before the redirect is fine.
    GET /health reports `paystack_mode` ("test" or "live").
    """
    booking = await Booking.get(ObjectId(booking_id))
    if not booking or str(booking.client.ref.id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    if booking.status not in (BookingStatus.ACCEPTED, BookingStatus.PENDING):
        raise HTTPException(
            status_code=400,
            detail="Booking must be accepted before escrow can be funded.",
        )
    if booking.escrow_status != EscrowStatus.UNFUNDED:
        raise HTTPException(status_code=400, detail="Escrow has already been funded.")

    amount = booking.escrow_amount or booking.amount
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid booking amount.")

    ref = f"KZ-ESCROW-{booking_id[:8]}-{ObjectId()}"
    tx = Transaction(
        booking=booking,
        transaction_reference=ref,
        amount=amount,
        type=TransactionType.ESCROW_DEPOSIT,
        status=TransactionStatus.PENDING,
    )
    await tx.insert()

    callback_url = None
    if settings.PAYSTACK_CALLBACK_URL:
        joiner = "&" if "?" in settings.PAYSTACK_CALLBACK_URL else "?"
        callback_url = f"{settings.PAYSTACK_CALLBACK_URL}{joiner}booking_id={booking.id}"

    try:
        data = await initialize_transaction(
            email=current_user.email,
            amount_kobo=int(round(amount * 100)),
            reference=ref,
            metadata={"booking_id": str(booking.id)},
            callback_url=callback_url,
        )
    except PaystackError as e:
        raise HTTPException(status_code=400, detail=f"Payment initialization failed: {e.client_message()}")

    return {
        "authorization_url": data["authorization_url"],
        "access_code": data["access_code"],
        "reference": ref,
        "callback_url": callback_url,
    }


def _webhook_event_id(event_type: str, data: dict) -> str:
    """Paystack doesn't send a single global webhook event ID the way some
    gateways do, so we derive a stable one from the event's own identifiers
    — good enough to dedupe retried deliveries of the *same* event."""
    return f"{event_type}:{data.get('id') or data.get('reference')}"


async def _handle_charge_success(data: dict) -> None:
    ref = data["reference"]
    tx = await Transaction.find_one(Transaction.transaction_reference == ref)
    if not (tx and tx.status == TransactionStatus.PENDING):
        return

    tx.status = TransactionStatus.SUCCESSFUL
    tx.gateway_response = data
    await tx.save()

    booking_id = tx.booking.ref.id if hasattr(tx.booking, "ref") else tx.booking.id
    booking = await Booking.get(booking_id)
    if not (booking and booking.escrow_status == EscrowStatus.UNFUNDED):
        return

    booking = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.ACCEPTED, BookingStatus.PENDING],
        allowed_escrow_statuses=[EscrowStatus.UNFUNDED],
        updates={
            "escrow_status": EscrowStatus.HELD_IN_ESCROW,
            "escrow_funded_at": tx.created_at,
            "payment_reference": ref,
        },
        to_status=BookingStatus.ESCROW_FUNDED,
    )

    # Counted here (real payment confirmed), not at booking creation —
    # security-audit fix: incrementing it earlier let anyone manufacture
    # fake "sold" counts for a gig without ever paying.
    if booking.booking_type == BookingType.GIG_PURCHASE and booking.gig_id:
        try:
            gig = await Gig.get(ObjectId(booking.gig_id))
            if gig:
                gig.orders_count += 1
                await gig.save()
        except Exception:
            pass

    conv = await Conversation.find_one({
        "client.$id": booking.client.ref.id,
        "artisan.$id": booking.artisan.ref.id
    })
    if conv:
        await manager.broadcast_to_conversation(str(conv.id), {
            "event": "escrow_funded",
            "booking_id": str(booking.id),
            "amount": booking.amount,
            "status": booking.status,
        })
    await notify_escrow_funded(booking)


async def _handle_transfer_event(event_type: str, data: dict) -> None:
    """Handles transfer.success/failed/reversed for an artisan payout
    initiated via app.services.payouts.release_escrow_with_payout.

    A failed/reversed transfer means the booking was already marked
    paid_out but the money never arrived — this flips the ledger entry to
    the true terminal status and flags it for follow-up rather than
    silently leaving the booking looking settled. Automatically re-queuing
    the payout for retry is Phase 10 (Webhook Retry Processor); for now
    this makes the discrepancy visible via audit_logs, a notification, and
    (per spec §11.6) via the ledger no longer summing to "successful".
    """
    ref = data.get("reference")
    tx = await Transaction.find_one({
        "transaction_reference": ref,
        "type": TransactionType.ESCROW_RELEASE.value,
    })
    if not tx:
        return

    if event_type == "transfer.success":
        tx.status = TransactionStatus.SUCCESSFUL
    elif event_type == "transfer.failed":
        tx.status = TransactionStatus.FAILED
    else:  # transfer.reversed
        tx.status = TransactionStatus.REVERSED
    tx.gateway_response = data
    await tx.save()

    if event_type in ("transfer.failed", "transfer.reversed"):
        booking_id = tx.booking.ref.id if hasattr(tx.booking, "ref") else tx.booking.id
        booking = await Booking.get(booking_id)
        if booking:
            artisan = await User.get(
                booking.artisan.ref.id if hasattr(booking.artisan, "ref") else booking.artisan.id
            )
            if artisan:
                await Notification(
                    user=artisan,
                    type="payout_issue",
                    title="Payout could not be completed",
                    message=(
                        "Your payout for a completed booking failed to reach your bank "
                        "account. Our team will follow up to resolve this."
                    ),
                    booking=booking,
                ).insert()
            await AuditLog(
                actor=artisan or booking.client,
                action="payout_failed" if event_type == "transfer.failed" else "payout_reversed",
                target_type="transaction",
                target_id=str(tx.id),
                reason=data.get("failures") or data.get("message"),
                metadata={"booking_id": str(booking.id), "reference": ref},
            ).insert()


async def dispatch_webhook_event(event_type: str, data: dict) -> None:
    """Routes a Paystack event to its handler. Shared by the live webhook
    route below and Phase 10's webhook-retry background job, so a queued
    retry runs the exact same logic as a first attempt."""
    if event_type == "charge.success":
        await _handle_charge_success(data)
    elif event_type in ("transfer.success", "transfer.failed", "transfer.reversed"):
        await _handle_transfer_event(event_type, data)


@router.post("/webhook/paystack")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(None)):
    """Paystack webhook. Order matters (spec §11.3): verify the signature
    before touching the DB at all, then check processed_webhook_events and
    short-circuit with 200 OK on a replayed delivery before doing any
    domain logic.
    """
    body = await request.body()
    hash_sig = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
        body,
        hashlib.sha256
    ).hexdigest()

    # FIX (security audit): a plain `!=` string comparison short-circuits on
    # the first differing byte, which is the textbook timing side-channel
    # for MAC/signature verification. hmac.compare_digest runs in constant
    # time regardless of where the strings first differ.
    if not hmac.compare_digest(hash_sig, x_paystack_signature or ""):
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_data = await request.json()
    event_type = event_data.get("event")
    data = event_data.get("data", {})

    # Claim this event atomically via the unique (gateway, event_id) index
    # *before* processing — if two deliveries of the same event race each
    # other, only one insert wins and the other short-circuits here. If our
    # own handler then fails, we own the retry (Phase 10's webhook-retry
    # job) rather than falling back to Paystack's redelivery — which the
    # dedup index would otherwise swallow as a no-op, since the claim
    # already exists.
    event_id = _webhook_event_id(event_type, data)
    record = ProcessedWebhookEvent(
        gateway="paystack",
        event_id=event_id,
        event_type=event_type or "unknown",
        status="processing",
        payload=event_data,
    )
    try:
        await record.insert()
    except DuplicateKeyError:
        # Replayed delivery of an event we've already claimed. Any other
        # insert failure (DB down, etc.) propagates as a 500 so Paystack
        # redelivers it instead of us silently dropping a real event.
        return {"status": "success"}

    try:
        await dispatch_webhook_event(event_type, data)
    except Exception as e:
        record.status = "failed_pending_retry"
        record.last_error = str(e)
        record.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        await record.save()
        return {"status": "success"}

    record.status = "completed"
    await record.save()
    return {"status": "success"}


@router.post("/release-escrow/{booking_id}")
async def release_escrow_to_artisan(
    booking_id: str,
    current_user: User = Depends(get_current_user),
):
    """Client releases funds to artisan upon job completion.

    Kept as a backward-compatible alias — prefer
    POST /api/v1/bookings/{id}/confirm-completion (spec-named, and requires
    an Idempotency-Key) for new integrations.
    """
    booking = await Booking.get(ObjectId(booking_id))
    if not booking or str(booking.client.ref.id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    booking = await core_confirm_completion(booking, current_user)

    return {"detail": "Funds released successfully.", "booking_status": booking.status}
