import asyncio
from datetime import datetime, timezone

import pytest

from app.core.security import get_password_hash
from app.models.bank_account import BankAccount
from app.models.booking import Booking, BookingStatus, EscrowStatus
from app.models.booking_status_history import BookingStatusHistory
from app.models.dispute import Dispute
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.services.booking_transitions import apply_booking_transition


async def give_verified_bank_account(artisan: User) -> None:
    await BankAccount(
        user=artisan,
        bank_code="058",
        bank_name="Test Bank",
        account_number="0123456789",
        account_name=f"{artisan.first_name} {artisan.last_name}",
        paystack_recipient_code="RCP_test",
        is_verified=True,
    ).insert()

pytestmark = pytest.mark.asyncio


async def make_user(email, role="client") -> User:
    user = User(
        first_name="A",
        last_name="B",
        email=email,
        phone_number=f"+234800000{hash(email) % 10000:04d}",
        state="Lagos",
        role=role,
        hashed_password=get_password_hash("Passw0rd!"),
    )
    await user.insert()
    return user


async def make_booking(client, artisan, **overrides) -> Booking:
    defaults = dict(
        client=client,
        artisan=artisan,
        title="Fix sink",
        amount=10000,
        escrow_amount=10000,
        status=BookingStatus.IN_PROGRESS,
        escrow_status=EscrowStatus.HELD_IN_ESCROW,
    )
    defaults.update(overrides)
    booking = Booking(**defaults)
    await booking.insert()
    return booking


async def test_transition_writes_history_row_in_same_call():
    client = await make_user("c1@example.com")
    artisan = await make_user("a1@example.com", role="artisan")
    booking = await make_booking(client, artisan)

    updated = await apply_booking_transition(
        booking.id,
        allowed_statuses=[BookingStatus.IN_PROGRESS],
        to_status=BookingStatus.COMPLETED_BY_ARTISAN,
        changed_by=artisan,
        reason="artisan submitted completion",
    )

    assert updated.status == BookingStatus.COMPLETED_BY_ARTISAN
    assert updated.lock_version == 1

    history = await BookingStatusHistory.find({"booking.$id": booking.id}).to_list()
    assert len(history) == 1
    assert history[0].from_status == "in_progress"
    assert history[0].to_status == "completed_by_artisan"


async def test_transition_rejects_wrong_starting_status():
    client = await make_user("c2@example.com")
    artisan = await make_user("a2@example.com", role="artisan")
    booking = await make_booking(client, artisan, status=BookingStatus.PENDING)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await apply_booking_transition(
            booking.id,
            allowed_statuses=[BookingStatus.IN_PROGRESS],
            to_status=BookingStatus.COMPLETED_BY_ARTISAN,
        )
    assert exc_info.value.status_code == 400


async def test_concurrent_transitions_only_one_wins_the_lock():
    """Simulates the exact race the spec is worried about: the auto-release
    cron and a client's dispute both trying to act on the same booking at
    once. Whichever acquires the lock_version-guarded write first should
    win; the other must see the now-updated status and fail its own
    precondition cleanly rather than double-transitioning the booking."""
    client = await make_user("c3@example.com")
    artisan = await make_user("a3@example.com", role="artisan")
    booking = await make_booking(client, artisan, status=BookingStatus.COMPLETED_BY_ARTISAN)

    async def try_release():
        return await apply_booking_transition(
            booking.id,
            allowed_statuses=[BookingStatus.COMPLETED_BY_ARTISAN],
            to_status=BookingStatus.PAID_OUT,
            reason="auto-release",
        )

    async def try_dispute():
        return await apply_booking_transition(
            booking.id,
            allowed_statuses=[BookingStatus.COMPLETED_BY_ARTISAN],
            to_status=BookingStatus.DISPUTED,
            reason="client disputed",
        )

    from fastapi import HTTPException

    results = await asyncio.gather(try_release(), try_dispute(), return_exceptions=True)

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, HTTPException)]

    # Exactly one transition succeeds; the other loses its precondition
    # check on retry (booking is no longer COMPLETED_BY_ARTISAN) rather than
    # silently overwriting the winner.
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status_code == 400

    final = await Booking.get(booking.id)
    assert final.status in (BookingStatus.PAID_OUT, BookingStatus.DISPUTED)
    # Exactly one history row was written for the winning transition (plus
    # none for the loser, since it never got past the precondition check).
    history = await BookingStatusHistory.find({"booking.$id": booking.id}).to_list()
    assert len(history) == 1
    assert history[0].to_status == final.status.value


async def test_confirm_completion_inserts_release_ledger_entry():
    from app.api.v1.endpoints.bookings import core_confirm_completion

    client = await make_user("c4@example.com")
    artisan = await make_user("a4@example.com", role="artisan")
    await give_verified_bank_account(artisan)
    booking = await make_booking(
        client, artisan, status=BookingStatus.COMPLETED_BY_ARTISAN, artisan_earnings=9000
    )

    updated = await core_confirm_completion(booking, client)

    assert updated.status == BookingStatus.PAID_OUT
    assert updated.escrow_status == EscrowStatus.RELEASED_TO_ARTISAN

    ledger = await Transaction.find({"booking.$id": booking.id}).to_list()
    assert len(ledger) == 1
    assert ledger[0].type == TransactionType.ESCROW_RELEASE
    # PENDING, not SUCCESSFUL: a real Paystack Transfer was just initiated —
    # final settlement is confirmed later by the transfer.success webhook.
    assert ledger[0].status == TransactionStatus.PENDING
    assert ledger[0].amount == 9000


async def test_dispute_creates_dispute_row_and_transitions_booking():
    from app.api.v1.endpoints.bookings import dispute_booking
    from app.models.dispute import DisputeCreate

    client = await make_user("c5@example.com")
    artisan = await make_user("a5@example.com", role="artisan")
    booking = await make_booking(client, artisan, status=BookingStatus.IN_PROGRESS)

    response = await dispute_booking(
        str(booking.id),
        DisputeCreate(reason="not_completed", details="Job was never finished."),
        current_user=client,
        idempotency_key="test-key-1",
    )

    assert response.status == "under_review"
    updated_booking = await Booking.get(booking.id)
    assert updated_booking.status == BookingStatus.DISPUTED

    disputes = await Dispute.find({"booking.$id": booking.id}).to_list()
    assert len(disputes) == 1
    assert disputes[0].reason == "not_completed"


async def test_dispute_idempotency_key_replays_without_creating_second_dispute():
    from app.api.v1.endpoints.bookings import dispute_booking
    from app.models.dispute import DisputeCreate

    client = await make_user("c6@example.com")
    artisan = await make_user("a6@example.com", role="artisan")
    booking = await make_booking(client, artisan, status=BookingStatus.IN_PROGRESS)

    payload = DisputeCreate(reason="quality", details="Poor workmanship.")
    first = await dispute_booking(
        str(booking.id), payload, current_user=client, idempotency_key="dup-key"
    )
    second = await dispute_booking(
        str(booking.id), payload, current_user=client, idempotency_key="dup-key"
    )

    # `second` comes back as the raw cached dict since we're calling the
    # endpoint function directly rather than through FastAPI's response
    # serialization — real HTTP callers get it re-validated into
    # DisputeResponse automatically via response_model.
    assert first.id == second["id"]
    disputes = await Dispute.find({"booking.$id": booking.id}).to_list()
    assert len(disputes) == 1
