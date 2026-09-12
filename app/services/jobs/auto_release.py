# app/services/jobs/auto_release.py
"""Hourly job: auto-releases escrow for bookings whose 4-day
completion-confirmation window has lapsed with no dispute (spec §6/§7.1).

Uses the same `apply_booking_transition` optimistic-lock helper as every
other booking mutation in this codebase (bookings.py, admin.py). That's
what gives us the spec's `SELECT ... FOR UPDATE SKIP LOCKED` behavior for
free: if a client's `POST /dispute` (or anything else) already moved this
booking's `lock_version` by the time this job tries to write, the
conditional update simply matches nothing, the helper's own retry loop
re-reads and finds the booking is no longer `completed_by_artisan`, and
raises — which this job treats as "skip it, someone else already acted,"
never as an error. A contested row never blocks the rest of the batch.
"""
import logging
from datetime import datetime, timezone

from fastapi import HTTPException

from app.models.booking import Booking, BookingStatus, EscrowStatus
from app.models.notification import Notification
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.services.booking_transitions import apply_booking_transition
from app.services.payouts import request_artisan_payout

logger = logging.getLogger("kazihub.jobs.auto_release")


async def _release_one(booking: Booking) -> None:
    artisan_id = booking.artisan.ref.id if hasattr(booking.artisan, "ref") else booking.artisan.id
    payout_amount = booking.artisan_earnings or booking.amount
    payout_result = await request_artisan_payout(
        artisan_id, payout_amount, reason=f"Auto-release for booking {booking.id}"
    )
    if payout_result["status"] != "pending":
        raise RuntimeError(
            f"Payout could not be initiated: {payout_result['gateway_response'].get('error')}"
        )

    async def _writes(b, session) -> None:
        await Transaction(
            booking=b,
            transaction_reference=payout_result["reference"],
            amount=payout_amount,
            type=TransactionType.ESCROW_RELEASE,
            status=TransactionStatus.PENDING,
            gateway_response=payout_result["gateway_response"],
        ).insert(session=session)

    updated = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.COMPLETED_BY_ARTISAN],
        allowed_escrow_statuses=[EscrowStatus.HELD_IN_ESCROW],
        updates={
            "escrow_status": EscrowStatus.RELEASED_TO_ARTISAN,
            "escrow_released_at": datetime.now(timezone.utc),
        },
        to_status=BookingStatus.PAID_OUT,
        reason="Auto-released after 4-day window with no dispute",
        extra_writes=_writes,
    )

    client_id = updated.client.ref.id if hasattr(updated.client, "ref") else updated.client.id
    client = await User.get(client_id)
    if client:
        await Notification(
            user=client,
            type="escrow_auto_released",
            title="Job auto-completed",
            message="Your booking was automatically marked complete and payment released to the artisan.",
            booking=updated,
        ).insert()


async def run_auto_release() -> dict:
    now = datetime.now(timezone.utc)
    candidates = await Booking.find(
        {
            "status": BookingStatus.COMPLETED_BY_ARTISAN.value,
            "auto_completion_deadline": {"$lte": now},
        }
    ).to_list()

    released, skipped, failed = 0, 0, 0
    for booking in candidates:
        try:
            await _release_one(booking)
            released += 1
        except HTTPException:
            # Lost the optimistic-lock race, or the booking no longer
            # qualifies by the time we tried to write (e.g. a dispute won
            # first) — skip, not an error. Picked up again next run if it
            # still qualifies.
            skipped += 1
        except RuntimeError as e:
            # Payout couldn't even be initiated (e.g. no verified bank
            # account) — leave the booking as-is for an admin to resolve
            # manually; don't mark it paid out with no money behind it.
            logger.warning("auto-release payout failed for booking=%s: %s", booking.id, e)
            failed += 1

    return {
        "candidates": len(candidates),
        "released": released,
        "skipped": skipped,
        "failed_payout": failed,
    }
