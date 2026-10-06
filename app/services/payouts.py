# app/services/payouts.py
"""Artisan disbursement (spec §11.4): triggers a real Paystack Transfer to
the artisan's verified bank account when escrow is released.

Ordering is the whole point of this module: the booking is *claimed* first
(an atomic, optimistic-locked transition to its released state, with a
PENDING ledger entry written in the same transaction), and only the winner
of that claim calls Paystack. Previously the transfer was initiated before
the transition, so two concurrent releases (client confirm vs. the
auto-release cron, a double-submitted confirm with different idempotency
keys, a dispute landing first) could each send real money while only one
of them managed to move the booking.

If Paystack definitively rejects the transfer, the claim is rolled back so
the booking can be released again once the problem is fixed. If the outcome
is unknown (network failure/timeout — Paystack may have accepted it), the
claim is deliberately *not* rolled back: the transfer reference is
idempotent at Paystack and the transfer.* webhook settles the ledger entry,
whereas rolling back would allow a second, duplicate transfer.
"""
import logging
import secrets
from typing import Awaitable, Callable, Optional, Sequence

from fastapi import HTTPException, status

from app.models.bank_account import BankAccount
from app.models.booking import Booking
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.services.booking_transitions import ExtraWrites, apply_booking_transition
from app.services.paystack import PaystackError, initiate_transfer

logger = logging.getLogger("kazihub.payouts")


class PayoutError(HTTPException):
    """Payout could not be initiated; the booking was left (or restored to)
    its pre-release state. Subclasses HTTPException so endpoints can let it
    propagate as a 400, while the auto-release job can still tell it apart
    from a lost lock race."""

    def __init__(self, detail: str):
        super().__init__(status.HTTP_400_BAD_REQUEST, detail=f"Payout failed: {detail}")


def _plain(value):
    return value.value if hasattr(value, "value") else value


async def _get_recipient_code(artisan_user_id) -> Optional[str]:
    bank_account = await BankAccount.find_one({"user.$id": artisan_user_id})
    if not bank_account or not bank_account.is_verified or not bank_account.paystack_recipient_code:
        return None
    return bank_account.paystack_recipient_code


async def release_escrow_with_payout(
    booking: Booking,
    *,
    payout_amount: float,
    payout_reason: str,
    allowed_statuses: Optional[Sequence[str]],
    allowed_escrow_statuses: Optional[Sequence[str]],
    updates: dict,
    to_status: str,
    changed_by: Optional[User] = None,
    reason: Optional[str] = None,
    extra_writes: Optional[ExtraWrites] = None,
    rollback_writes: Optional[Callable[[Booking, object], Awaitable[None]]] = None,
) -> Booking:
    """Claim the booking's release, then pay the artisan.

    `extra_writes` runs inside the claiming transaction (alongside the
    PENDING escrow_release ledger entry); `rollback_writes` runs inside the
    rollback transaction if Paystack rejects the transfer, to undo whatever
    `extra_writes` changed outside the booking itself.

    Raises PayoutError (a 400) if the payout couldn't be initiated, and the
    usual HTTPException from apply_booking_transition if the claim loses.
    """
    artisan_id = booking.artisan.ref.id if hasattr(booking.artisan, "ref") else booking.artisan.id

    # Refuse a booking in the wrong state before anything else — before the
    # bank-account lookup, the ledger, or Paystack (frontend ask 23: an
    # unfunded booking must fail with a status error, not a payout error).
    current = await Booking.get(booking.id)
    if not current:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Booking not found.")
    if allowed_statuses is not None and _plain(current.status) not in [_plain(x) for x in allowed_statuses]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Booking status '{_plain(current.status)}' cannot make this transition.",
        )
    if allowed_escrow_statuses is not None and _plain(current.escrow_status) not in [
        _plain(x) for x in allowed_escrow_statuses
    ]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Escrow status '{_plain(current.escrow_status)}' cannot make this transition.",
        )
    booking = current

    # Cheap pre-check before claiming, so the common "no bank account yet"
    # case never touches the booking at all.
    recipient_code = await _get_recipient_code(artisan_id)
    if not recipient_code:
        raise PayoutError("Artisan has no verified payout bank account on file.")

    reference = f"KZ-PAYOUT-{secrets.token_hex(6).upper()}"
    prior_status = _plain(booking.status)
    prior_escrow_status = _plain(booking.escrow_status)

    async def _claim_writes(b: Booking, session) -> None:
        await Transaction(
            booking=b,
            transaction_reference=reference,
            amount=payout_amount,
            type=TransactionType.ESCROW_RELEASE,
            status=TransactionStatus.PENDING,
        ).insert(session=session)
        if extra_writes is not None:
            await extra_writes(b, session)

    claimed = await apply_booking_transition(
        booking.id,
        allowed_statuses=allowed_statuses,
        allowed_escrow_statuses=allowed_escrow_statuses,
        updates=updates,
        to_status=to_status,
        changed_by=changed_by,
        reason=reason,
        extra_writes=_claim_writes,
    )

    tx = await Transaction.find_one({"transaction_reference": reference})

    try:
        response = await initiate_transfer(
            amount_kobo=int(round(payout_amount * 100)),
            recipient_code=recipient_code,
            reason=payout_reason,
            reference=reference,
        )
    except PaystackError as e:
        if not e.safe_to_expose:
            # Outcome unknown — keep the claim; the webhook settles it.
            logger.error(
                "Payout outcome unknown for booking=%s reference=%s: %s",
                booking.id, reference, e,
            )
            if tx:
                tx.gateway_response = {"error": str(e), "outcome_unknown": True}
                await tx.save()
            return claimed

        if tx:
            tx.status = TransactionStatus.FAILED
            tx.gateway_response = {"error": str(e), **e.response_data}
            await tx.save()

        await apply_booking_transition(
            booking.id,
            allowed_statuses=[_plain(to_status)],
            allowed_escrow_statuses=[_plain(updates.get("escrow_status", prior_escrow_status))],
            updates={"escrow_status": prior_escrow_status, "escrow_released_at": None},
            to_status=prior_status,
            changed_by=changed_by,
            reason=f"Payout rejected by Paystack; release rolled back ({e})",
            extra_writes=rollback_writes,
        )
        raise PayoutError(e.client_message())

    if tx:
        tx.gateway_response = response
        await tx.save()
    return claimed
