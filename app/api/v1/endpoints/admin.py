# app/api/v1/endpoints/admin.py
from datetime import datetime, timezone
from typing import List, Optional

from beanie import PydanticObjectId
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_admin
from app.api.v1.endpoints.auth import revoke_all_sessions_for
from app.api.v1.endpoints.bookings import booking_response
from app.core.idempotency import cache_response, get_cached_response, require_idempotency_key
from app.core.two_factor import verify_admin_totp
from app.models.audit_log import AuditLog, AuditLogResponse
from app.models.booking import Booking, BookingResponse, BookingStatus, EscrowStatus
from app.api.v1.endpoints.support import build_ticket_response
from app.core.time import utc_now
from app.models.dispute import Dispute, DisputeResponse
from app.models.review import Review
from app.models.support_ticket import SupportTicket, SupportTicketResponse
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.models.verification import VerificationStatus, Verification
from app.services.booking_transitions import apply_booking_transition
from app.services.notifications import notify, notify_booking_party
from app.services.payouts import release_escrow_with_payout

router = APIRouter()


def build_dispute_response(d: Dispute) -> DisputeResponse:
    return DisputeResponse(
        id=str(d.id),
        ticket_id=d.ticket_id,
        booking_id=str(d.booking.ref.id if hasattr(d.booking, "ref") else d.booking.id),
        client_id=str(d.client.ref.id if hasattr(d.client, "ref") else d.client.id),
        artisan_id=str(d.artisan.ref.id if hasattr(d.artisan, "ref") else d.artisan.id),
        reason=d.reason,
        details=d.details,
        evidence_photos=d.evidence_photos,
        status=d.status,
        resolution_notes=d.resolution_notes,
        resolved_at=d.resolved_at,
        created_at=d.created_at,
    )


def build_audit_log_response(a: AuditLog) -> AuditLogResponse:
    return AuditLogResponse(
        id=str(a.id),
        actor_id=str(a.actor.ref.id if hasattr(a.actor, "ref") else a.actor.id),
        action=a.action,
        target_type=a.target_type,
        target_id=a.target_id,
        reason=a.reason,
        metadata=a.metadata,
        created_at=a.created_at,
    )


async def _load_booking_or_404(booking_id: str) -> Booking:
    try:
        booking = await Booking.get(PydanticObjectId(booking_id))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid booking ID.")
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Booking not found.")
    return booking


# ---------------------------------------------------------
# DISPUTES
# ---------------------------------------------------------
@router.get("/disputes", response_model=List[DisputeResponse])
async def list_disputes(
    dispute_status: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_admin: User = Depends(get_current_admin),
):
    query: dict = {}
    if dispute_status:
        query["status"] = dispute_status
    disputes = (
        await Dispute.find(query).sort("-created_at").skip(offset).limit(limit).to_list()
    )
    return [build_dispute_response(d) for d in disputes]


class DisputeResolveRequest(BaseModel):
    resolution: str  # "client_refund" | "artisan_paid" | "split"
    split_ratio: Optional[float] = Field(default=None, gt=0, lt=1)
    resolution_notes: Optional[str] = None
    totp_code: str


@router.post("/disputes/{dispute_id}/resolve", response_model=DisputeResponse)
async def resolve_dispute(
    dispute_id: str,
    payload: DisputeResolveRequest,
    current_admin: User = Depends(get_current_admin),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Resolve a dispute, executing the corresponding escrow transfer/refund
    and writing audit_logs. Requires a fresh 2FA code with each call, even
    though the admin already has 2FA enabled to reach this endpoint at all.
    """
    cached = await get_cached_response(idempotency_key, current_admin, "admin.resolve_dispute")
    if cached:
        return cached

    verify_admin_totp(current_admin, payload.totp_code)

    if payload.resolution not in ("client_refund", "artisan_paid", "split"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="resolution must be one of: client_refund, artisan_paid, split.",
        )
    if payload.resolution == "split" and not payload.split_ratio:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="split_ratio is required for a split resolution."
        )

    try:
        dispute = await Dispute.get(PydanticObjectId(dispute_id))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid dispute ID.")
    if not dispute:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Dispute not found.")
    if dispute.status not in ("under_review", "artisan_responding", "arbitration"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"Dispute is already resolved ({dispute.status})."
        )

    booking_id = dispute.booking.ref.id if hasattr(dispute.booking, "ref") else dispute.booking.id
    booking = await Booking.get(booking_id)
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Booking not found.")

    escrow_amount = booking.escrow_amount or booking.amount

    if payload.resolution == "client_refund":
        refund_amount, release_amount = escrow_amount, 0.0
        new_escrow_status, new_booking_status = EscrowStatus.REFUNDED_TO_CLIENT, BookingStatus.CANCELLED
        new_dispute_status = "resolved_client_refund"
    elif payload.resolution == "artisan_paid":
        refund_amount, release_amount = 0.0, escrow_amount
        new_escrow_status, new_booking_status = EscrowStatus.RELEASED_TO_ARTISAN, BookingStatus.PAID_OUT
        new_dispute_status = "resolved_artisan_paid"
    else:
        release_amount = round(escrow_amount * payload.split_ratio, 2)
        refund_amount = round(escrow_amount - release_amount, 2)
        new_escrow_status, new_booking_status = EscrowStatus.PARTIALLY_REFUNDED, BookingStatus.CANCELLED
        new_dispute_status = "resolved_split"

    prior_dispute_status = dispute.status
    refund_reference = f"KZ-DISPUTE-REFUND-{str(booking.id)[:8]}-{ObjectId()}"

    async def _writes(b: Booking, session) -> None:
        if refund_amount > 0:
            await Transaction(
                booking=b,
                transaction_reference=refund_reference,
                amount=refund_amount,
                type=TransactionType.REFUND,
                status=TransactionStatus.SUCCESSFUL,
            ).insert(session=session)
        dispute.status = new_dispute_status
        dispute.resolution_notes = payload.resolution_notes
        dispute.resolved_by = current_admin
        dispute.resolved_at = datetime.now(timezone.utc)
        await dispute.save(session=session)

        await AuditLog(
            actor=current_admin,
            action="dispute_resolved",
            target_type="dispute",
            target_id=str(dispute.id),
            reason=payload.resolution_notes,
            metadata={
                "resolution": payload.resolution,
                "refund_amount": refund_amount,
                "release_amount": release_amount,
            },
        ).insert(session=session)

    transition_kwargs = dict(
        allowed_statuses=[BookingStatus.DISPUTED],
        allowed_escrow_statuses=None,
        updates={
            "escrow_status": new_escrow_status,
            "escrow_released_at": datetime.now(timezone.utc),
        },
        to_status=new_booking_status,
        changed_by=current_admin,
        reason=f"Dispute resolved: {payload.resolution}",
        extra_writes=_writes,
    )

    if release_amount > 0:
        # A payout portion (artisan_paid / split) triggers a real Paystack
        # Transfer — claimed first, paid second (see app.services.payouts).
        # If Paystack rejects it, the booking goes back to disputed and the
        # dispute is reopened, and the error surfaces here as a 400.
        async def _rollback(b: Booking, session) -> None:
            dispute.status = prior_dispute_status
            dispute.resolution_notes = None
            dispute.resolved_by = None
            dispute.resolved_at = None
            await dispute.save(session=session)
            refund_tx = await Transaction.find_one({"transaction_reference": refund_reference})
            if refund_tx:
                refund_tx.status = TransactionStatus.FAILED
                await refund_tx.save(session=session)
            await AuditLog(
                actor=current_admin,
                action="dispute_resolution_rolled_back",
                target_type="dispute",
                target_id=str(dispute.id),
                reason="Paystack rejected the artisan payout.",
            ).insert(session=session)

        await release_escrow_with_payout(
            booking,
            payout_amount=release_amount,
            payout_reason=f"Dispute resolution payout for booking {booking.id}",
            rollback_writes=_rollback,
            **transition_kwargs,
        )
    else:
        await apply_booking_transition(booking.id, **transition_kwargs)

    outcome = {
        "client_refund": "The client was refunded in full.",
        "artisan_paid": "The payment was released to the artisan.",
        "split": f"The payment was split: {release_amount:,.2f} NGN to the artisan, {refund_amount:,.2f} NGN refunded.",
    }[payload.resolution]
    resolved_booking = await Booking.get(booking.id)
    for who in ("client", "artisan"):
        await notify_booking_party(
            resolved_booking, who, "dispute_resolved", "Dispute resolved",
            f"The dispute on '{booking.title}' ({dispute.ticket_id}) was resolved. {outcome}",
        )

    updated_dispute = await Dispute.get(dispute.id)
    response = build_dispute_response(updated_dispute)
    await cache_response(
        idempotency_key, current_admin, "admin.resolve_dispute", response.model_dump(mode="json"), 200
    )
    return response


# ---------------------------------------------------------
# MANUAL ESCROW OVERRIDES
# ---------------------------------------------------------
class ForceRefundRequest(BaseModel):
    amount: float = Field(gt=0)
    reason: str
    totp_code: str


@router.post("/bookings/{booking_id}/force-refund", response_model=BookingResponse)
async def force_refund_booking(
    booking_id: str,
    payload: ForceRefundRequest,
    current_admin: User = Depends(get_current_admin),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Manual override outside the normal dispute flow."""
    cached = await get_cached_response(idempotency_key, current_admin, "admin.force_refund")
    if cached:
        return cached

    verify_admin_totp(current_admin, payload.totp_code)

    booking = await _load_booking_or_404(booking_id)
    if booking.escrow_status not in (EscrowStatus.HELD_IN_ESCROW, EscrowStatus.PARTIALLY_REFUNDED):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Escrow must be funded to force a refund."
        )
    escrow_amount = booking.escrow_amount or booking.amount
    if payload.amount > escrow_amount:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Refund amount exceeds escrow amount.")

    full_refund = payload.amount >= escrow_amount
    new_escrow_status = (
        EscrowStatus.REFUNDED_TO_CLIENT if full_refund else EscrowStatus.PARTIALLY_REFUNDED
    )

    async def _writes(b: Booking, session) -> None:
        await Transaction(
            booking=b,
            transaction_reference=f"KZ-FORCE-REFUND-{str(b.id)[:8]}-{ObjectId()}",
            amount=payload.amount,
            type=TransactionType.REFUND,
            status=TransactionStatus.SUCCESSFUL,
        ).insert(session=session)
        await AuditLog(
            actor=current_admin,
            action="escrow_force_refunded",
            target_type="booking",
            target_id=str(b.id),
            reason=payload.reason,
            metadata={"amount": payload.amount},
        ).insert(session=session)

    booking = await apply_booking_transition(
        booking.id,
        updates={
            "escrow_status": new_escrow_status,
            "escrow_released_at": datetime.now(timezone.utc),
        },
        to_status=BookingStatus.CANCELLED if full_refund else None,
        changed_by=current_admin,
        reason=f"Admin force-refund: {payload.reason}",
        extra_writes=_writes,
    )

    await notify_booking_party(
        booking, "client", "booking_cancelled" if full_refund else "dispute_resolved",
        "Refund issued", f"An admin refunded {payload.amount:,.2f} NGN on '{booking.title}'.",
    )
    response = await booking_response(booking, None)
    await cache_response(
        idempotency_key, current_admin, "admin.force_refund", response.model_dump(mode="json"), 200
    )
    return response


class ForceReleaseRequest(BaseModel):
    reason: str
    totp_code: str


@router.post("/bookings/{booking_id}/force-release", response_model=BookingResponse)
async def force_release_booking(
    booking_id: str,
    payload: ForceReleaseRequest,
    current_admin: User = Depends(get_current_admin),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Manual override outside the normal completion flow."""
    cached = await get_cached_response(idempotency_key, current_admin, "admin.force_release")
    if cached:
        return cached

    verify_admin_totp(current_admin, payload.totp_code)

    booking = await _load_booking_or_404(booking_id)
    if booking.escrow_status != EscrowStatus.HELD_IN_ESCROW:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Escrow must be funded and held to force a release."
        )
    release_amount = booking.escrow_amount or booking.amount

    async def _writes(b: Booking, session) -> None:
        await AuditLog(
            actor=current_admin,
            action="escrow_force_released",
            target_type="booking",
            target_id=str(b.id),
            reason=payload.reason,
            metadata={"amount": release_amount},
        ).insert(session=session)

    booking = await release_escrow_with_payout(
        booking,
        payout_amount=release_amount,
        payout_reason=f"Admin force-release for booking {booking.id}",
        allowed_statuses=None,
        allowed_escrow_statuses=[EscrowStatus.HELD_IN_ESCROW],
        updates={
            "escrow_status": EscrowStatus.RELEASED_TO_ARTISAN,
            "escrow_released_at": datetime.now(timezone.utc),
        },
        to_status=BookingStatus.PAID_OUT,
        changed_by=current_admin,
        reason=f"Admin force-release: {payload.reason}",
        extra_writes=_writes,
    )

    await notify_booking_party(
        booking, "artisan", "payment_released", "Payment released",
        f"An admin released the payment for '{booking.title}' to your bank account.",
    )
    response = await booking_response(booking, None)
    await cache_response(
        idempotency_key, current_admin, "admin.force_release", response.model_dump(mode="json"), 200
    )
    return response


# ---------------------------------------------------------
# USER SUSPENSION
# ---------------------------------------------------------
class SuspendUserRequest(BaseModel):
    reason: str
    totp_code: str


class ReactivateUserRequest(BaseModel):
    reason: Optional[str] = None
    totp_code: str


@router.post("/users/{user_id}/suspend")
async def suspend_user(
    user_id: str,
    payload: SuspendUserRequest,
    current_admin: User = Depends(get_current_admin),
):
    verify_admin_totp(current_admin, payload.totp_code)

    try:
        target = await User.get(PydanticObjectId(user_id))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid user ID.")
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")
    if target.is_admin:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Cannot suspend an admin account through this endpoint."
        )

    target.is_active = False
    target.is_frozen = True
    target.token_version += 1
    await target.save()
    await revoke_all_sessions_for(target)

    await AuditLog(
        actor=current_admin,
        action="user_suspended",
        target_type="user",
        target_id=str(target.id),
        reason=payload.reason,
    ).insert()

    return {"detail": "User suspended."}


@router.post("/users/{user_id}/reactivate")
async def reactivate_user(
    user_id: str,
    payload: ReactivateUserRequest,
    current_admin: User = Depends(get_current_admin),
):
    verify_admin_totp(current_admin, payload.totp_code)

    try:
        target = await User.get(PydanticObjectId(user_id))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid user ID.")
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    target.is_active = True
    target.is_frozen = False
    target.token_version += 1
    await target.save()

    await AuditLog(
        actor=current_admin,
        action="user_reactivated",
        target_type="user",
        target_id=str(target.id),
        reason=payload.reason,
    ).insert()

    return {"detail": "User reactivated."}


# ---------------------------------------------------------
# AUDIT LOGS & ANALYTICS
# ---------------------------------------------------------
@router.get("/audit-logs", response_model=List[AuditLogResponse])
async def list_audit_logs(
    actor_id: Optional[str] = None,
    target_type: Optional[str] = None,
    action: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_admin: User = Depends(get_current_admin),
):
    query: dict = {}
    if actor_id:
        try:
            query["actor.$id"] = PydanticObjectId(actor_id)
        except Exception:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid actor_id.")
    if target_type:
        query["target_type"] = target_type
    if action:
        query["action"] = action
    if date_from or date_to:
        date_filter: dict = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        query["created_at"] = date_filter

    logs = (
        await AuditLog.find(query).sort("-created_at").skip(offset).limit(limit).to_list()
    )
    return [build_audit_log_response(a) for a in logs]


@router.get("/analytics/overview")
async def analytics_overview(current_admin: User = Depends(get_current_admin)):
    successful_tx = await Transaction.find(
        {"status": TransactionStatus.SUCCESSFUL.value}
    ).to_list()

    volume_by_type: dict = {}
    for tx in successful_tx:
        key = tx.type.value if hasattr(tx.type, "value") else tx.type
        volume_by_type[key] = volume_by_type.get(key, 0.0) + tx.amount

    active_disputes = await Dispute.find(
        {"status": {"$in": ["under_review", "artisan_responding", "arbitration"]}}
    ).count()
    kyc_queue_depth = await Verification.find(
        {"status": VerificationStatus.PENDING.value}
    ).count()

    return {
        "transaction_volume_by_type": volume_by_type,
        "total_transaction_volume": sum(volume_by_type.values()),
        "active_disputes": active_disputes,
        "kyc_queue_depth": kyc_queue_depth,
    }


# ---------------------------------------------------------
# LANDING-PAGE REVIEWS (frontend ask 40)
# ---------------------------------------------------------
class FeatureReviewRequest(BaseModel):
    featured: bool


@router.post("/reviews/{review_id}/feature")
async def feature_review(
    review_id: str,
    payload: FeatureReviewRequest,
    current_admin: User = Depends(get_current_admin),
):
    """Pin (or unpin) a review at the top of GET /reviews/featured. Only
    reviews the client agreed to share can be featured."""
    try:
        review = await Review.get(PydanticObjectId(review_id))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid review ID.")
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Review not found.")
    if payload.featured and not review.share_publicly:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="The client hasn't agreed to share this review publicly."
        )
    review.is_featured = payload.featured
    await review.save()
    await AuditLog(
        actor=current_admin,
        action="review_featured" if payload.featured else "review_unfeatured",
        target_type="review",
        target_id=str(review.id),
    ).insert()
    return {"detail": "Review featured." if payload.featured else "Review unfeatured."}


# ---------------------------------------------------------
# SUPPORT TICKETS (frontend ask 14)
# ---------------------------------------------------------
class SupportTicketStatusUpdate(BaseModel):
    status: str = Field(pattern="^(open|in_progress|resolved|closed)$")


@router.get("/support-tickets", response_model=List[SupportTicketResponse])
async def list_support_tickets(
    ticket_status: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_admin: User = Depends(get_current_admin),
):
    query = {"status": ticket_status} if ticket_status else {}
    tickets = await SupportTicket.find(query).sort("-created_at").skip(offset).limit(limit).to_list()
    return [build_ticket_response(t) for t in tickets]


@router.patch("/support-tickets/{ticket_id}", response_model=SupportTicketResponse)
async def update_support_ticket(
    ticket_id: str,
    payload: SupportTicketStatusUpdate,
    current_admin: User = Depends(get_current_admin),
):
    try:
        ticket = await SupportTicket.get(PydanticObjectId(ticket_id))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid ticket ID.")
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Ticket not found.")
    ticket.status = payload.status
    ticket.updated_at = utc_now()
    await ticket.save()
    owner_id = ticket.user.ref.id if hasattr(ticket.user, "ref") else ticket.user.id
    await notify(
        owner_id, "support_ticket_update", "Support request updated",
        f"Your request {ticket.ticket_number} is now {payload.status.replace('_', ' ')}.",
    )
    return build_ticket_response(ticket)
