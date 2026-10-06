"""Claim-then-pay ordering for escrow release (app.services.payouts)."""
import asyncio

import pytest
from fastapi import HTTPException

from app.core.security import get_password_hash
from app.models.bank_account import BankAccount
from app.models.booking import Booking, BookingStatus, EscrowStatus
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.services.payouts import PayoutError, release_escrow_with_payout
from app.services.paystack import PaystackError

pytestmark = pytest.mark.asyncio


async def make_user(email, role="client") -> User:
    user = User(
        first_name="A",
        last_name="B",
        email=email,
        phone_number=f"+234800000{abs(hash(email)) % 10000:04d}",
        state="Lagos",
        role=role,
        hashed_password=get_password_hash("Passw0rd!"),
    )
    await user.insert()
    return user


async def make_releasable_booking(prefix: str) -> Booking:
    client = await make_user(f"{prefix}c@example.com")
    artisan = await make_user(f"{prefix}a@example.com", role="artisan")
    await BankAccount(
        user=artisan, bank_code="058", bank_name="Test Bank", account_number="0123456789",
        account_name="A B", paystack_recipient_code="RCP_test", is_verified=True,
    ).insert()
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000, escrow_amount=5000,
        artisan_earnings=4500, status=BookingStatus.COMPLETED_BY_ARTISAN,
        escrow_status=EscrowStatus.HELD_IN_ESCROW,
    )
    await booking.insert()
    return booking


def release(booking: Booking):
    return release_escrow_with_payout(
        booking,
        payout_amount=4500,
        payout_reason="test",
        allowed_statuses=[BookingStatus.COMPLETED_BY_ARTISAN],
        allowed_escrow_statuses=[EscrowStatus.HELD_IN_ESCROW],
        updates={"escrow_status": EscrowStatus.RELEASED_TO_ARTISAN},
        to_status=BookingStatus.PAID_OUT,
    )


def stub_transfer(monkeypatch, behaviour=None):
    calls = []

    async def _fake(amount_kobo, recipient_code, reason, reference):
        calls.append(reference)
        if behaviour:
            behaviour()
        return {"transfer_code": "TRF_test", "reference": reference}

    monkeypatch.setattr("app.services.payouts.initiate_transfer", _fake)
    return calls


async def test_second_release_of_same_booking_never_sends_a_second_transfer(monkeypatch):
    calls = stub_transfer(monkeypatch)
    booking = await make_releasable_booking("dp")
    stale_copy = await Booking.get(booking.id)

    await release(booking)
    with pytest.raises(HTTPException):
        await release(stale_copy)

    assert len(calls) == 1
    txs = await Transaction.find({"type": TransactionType.ESCROW_RELEASE.value}).to_list()
    assert len(txs) == 1
    assert txs[0].transaction_reference == calls[0]


async def test_concurrent_releases_send_exactly_one_transfer(monkeypatch):
    calls = stub_transfer(monkeypatch)
    booking = await make_releasable_booking("cc")
    copies = [await Booking.get(booking.id) for _ in range(3)]

    results = await asyncio.gather(*(release(b) for b in copies), return_exceptions=True)

    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert len(calls) == 1


async def test_rejected_transfer_rolls_booking_back_and_fails_ledger_entry(monkeypatch):
    def _reject():
        raise PaystackError("Insufficient balance", {}, safe_to_expose=True)

    stub_transfer(monkeypatch, _reject)
    booking = await make_releasable_booking("rj")

    with pytest.raises(PayoutError):
        await release(booking)

    restored = await Booking.get(booking.id)
    assert restored.status == BookingStatus.COMPLETED_BY_ARTISAN
    assert restored.escrow_status == EscrowStatus.HELD_IN_ESCROW
    tx = await Transaction.find_one({"type": TransactionType.ESCROW_RELEASE.value})
    assert tx.status == TransactionStatus.FAILED


async def test_unknown_transfer_outcome_keeps_claim_to_avoid_double_pay(monkeypatch):
    def _timeout():
        raise PaystackError("Network error contacting Paystack", safe_to_expose=False)

    stub_transfer(monkeypatch, _timeout)
    booking = await make_releasable_booking("to")

    updated = await release(booking)

    assert updated.status == BookingStatus.PAID_OUT
    tx = await Transaction.find_one({"type": TransactionType.ESCROW_RELEASE.value})
    assert tx.status == TransactionStatus.PENDING
    assert tx.gateway_response["outcome_unknown"] is True


async def test_missing_bank_account_fails_without_touching_booking():
    client = await make_user("nbc@example.com")
    artisan = await make_user("nba@example.com", role="artisan")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000,
        status=BookingStatus.COMPLETED_BY_ARTISAN, escrow_status=EscrowStatus.HELD_IN_ESCROW,
    )
    await booking.insert()

    with pytest.raises(PayoutError):
        await release(booking)

    unchanged = await Booking.get(booking.id)
    assert unchanged.lock_version == booking.lock_version
    assert await Transaction.find_one({}) is None


async def test_created_at_is_set_per_document_not_at_import():
    first = await make_user("ts1@example.com")
    await asyncio.sleep(0.01)
    second = await make_user("ts2@example.com")
    assert second.created_at > first.created_at


async def test_duplicate_webhook_claim_raises_duplicate_key_error():
    """paystack_webhook treats only DuplicateKeyError as a replay; any other
    insert failure must propagate so Paystack redelivers the event."""
    from pymongo.errors import DuplicateKeyError

    from app.models.webhook_event import ProcessedWebhookEvent

    await ProcessedWebhookEvent(gateway="paystack", event_id="charge.success:1", event_type="charge.success").insert()
    with pytest.raises(DuplicateKeyError):
        await ProcessedWebhookEvent(gateway="paystack", event_id="charge.success:1", event_type="charge.success").insert()
