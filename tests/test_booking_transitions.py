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


async def test_client_cannot_self_cancel_a_disputed_booking_for_a_refund():
    """Security-audit fix: a client used to be able to dispute a booking
    and then immediately self-cancel it for a full refund, completely
    bypassing admin adjudication of the dispute."""
    from fastapi import HTTPException

    from app.api.v1.endpoints.bookings import cancel_booking

    client = await make_user("cancelbypass_c@example.com")
    artisan = await make_user("cancelbypass_a@example.com", role="artisan")
    booking = await make_booking(
        client, artisan, status=BookingStatus.DISPUTED, escrow_status=EscrowStatus.HELD_IN_ESCROW
    )

    with pytest.raises(HTTPException) as exc_info:
        await cancel_booking(str(booking.id), current_user=client)
    assert exc_info.value.status_code == 400

    unchanged = await Booking.get(booking.id)
    assert unchanged.status == BookingStatus.DISPUTED
    assert unchanged.escrow_status == EscrowStatus.HELD_IN_ESCROW


async def test_buy_gig_ignores_client_supplied_amount_and_uses_gig_price():
    """Security-audit fix: buy_gig_item used to trust a client-supplied
    `amount` with no cross-check against the gig's real price, letting a
    client buy any gig for an arbitrary amount."""
    from app.api.v1.endpoints.bookings import buy_gig_item
    from app.api.v1.endpoints.gigs import create_gig
    from app.models.booking import GigPurchaseCreate
    from app.models.gig import GigCreate
    from app.models.profile import Profile

    client = await make_user("gigprice_c@example.com")
    artisan = await make_user("gigprice_a@example.com", role="artisan")
    profile = Profile(user=artisan, category="carpentry", state="Lagos")
    await profile.insert()

    gig = await create_gig(
        GigCreate(
            title="Custom bookshelf",
            description="Handmade oak bookshelf",
            category="carpentry",
            price=50000,
            delivery_time_days=5,
        ),
        profile=profile,
    )

    payload_dict = {
        "artisan_id": str(artisan.id),
        "gig_id": gig.id,
        "item_title": "Custom bookshelf",
        "delivery_address": "12 Example Street",
    }
    booking = await buy_gig_item(
        GigPurchaseCreate(**payload_dict),
        current_user=client,
        idempotency_key="gig-price-test",
    )

    assert booking.amount == 50000
    assert booking.escrow_amount == 50000


async def test_gig_orders_count_only_increments_on_real_payment_not_booking_creation():
    """Security-audit fix: orders_count used to increment the instant a
    booking was created, before any money moved — letting anyone
    manufacture fake 'sold' counts for free by creating and abandoning
    bookings. It must only increment once escrow is actually funded."""
    from app.api.v1.endpoints.bookings import buy_gig_item
    from app.api.v1.endpoints.gigs import create_gig
    from app.api.v1.endpoints.wallet import _handle_charge_success
    from app.models.booking import GigPurchaseCreate
    from app.models.gig import Gig, GigCreate
    from app.models.profile import Profile
    from app.models.transaction import Transaction, TransactionStatus, TransactionType

    client = await make_user("gigcount_c@example.com")
    artisan = await make_user("gigcount_a@example.com", role="artisan")
    profile = Profile(user=artisan, category="carpentry", state="Lagos")
    await profile.insert()

    gig = await create_gig(
        GigCreate(
            title="Custom shelf", description="desc", category="carpentry",
            price=20000, delivery_time_days=3,
        ),
        profile=profile,
    )

    booking = await buy_gig_item(
        GigPurchaseCreate(
            artisan_id=str(artisan.id), gig_id=gig.id, item_title="Custom shelf",
            delivery_address="1 Example Rd",
        ),
        current_user=client,
        idempotency_key="gig-orders-count-test",
    )

    # No payment has happened yet — orders_count must still be 0.
    unpaid_gig = await Gig.get(gig.id)
    assert unpaid_gig.orders_count == 0

    booking_doc = await Booking.get(booking.id)
    tx = Transaction(
        booking=booking_doc,
        transaction_reference="KZ-ESCROW-gigcount",
        amount=20000,
        type=TransactionType.ESCROW_DEPOSIT,
        status=TransactionStatus.PENDING,
    )
    await tx.insert()

    await _handle_charge_success({"reference": "KZ-ESCROW-gigcount", "id": 1})

    paid_gig = await Gig.get(gig.id)
    assert paid_gig.orders_count == 1
