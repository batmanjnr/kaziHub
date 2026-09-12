import pytest

from app.api.v1.endpoints.wallet import (
    _handle_charge_success,
    _handle_transfer_event,
    _webhook_event_id,
)
from app.core.security import get_password_hash
from app.models.audit_log import AuditLog
from app.models.booking import Booking, BookingStatus, EscrowStatus
from app.models.notification import Notification
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.models.webhook_event import ProcessedWebhookEvent

# pytest.ini sets asyncio_mode = auto; the one sync test below is unmarked.


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


def test_webhook_event_id_is_stable_and_derived_from_data():
    data = {"id": 12345, "reference": "KZ-ESCROW-abc"}
    assert _webhook_event_id("charge.success", data) == "charge.success:12345"

    data_no_id = {"reference": "KZ-ESCROW-xyz"}
    assert _webhook_event_id("charge.success", data_no_id) == "charge.success:KZ-ESCROW-xyz"


async def test_processed_webhook_event_dedup_via_unique_index():
    await ProcessedWebhookEvent(
        gateway="paystack", event_id="charge.success:1", event_type="charge.success"
    ).insert()

    with pytest.raises(Exception):
        await ProcessedWebhookEvent(
            gateway="paystack", event_id="charge.success:1", event_type="charge.success"
        ).insert()


async def test_handle_charge_success_funds_escrow():
    client = await make_user("wc1@example.com")
    artisan = await make_user("wa1@example.com", role="artisan")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000, escrow_amount=5000,
        status=BookingStatus.ACCEPTED, escrow_status=EscrowStatus.UNFUNDED,
    )
    await booking.insert()
    tx = Transaction(
        booking=booking, transaction_reference="KZ-ESCROW-test1", amount=5000,
        type=TransactionType.ESCROW_DEPOSIT, status=TransactionStatus.PENDING,
    )
    await tx.insert()

    await _handle_charge_success({"reference": "KZ-ESCROW-test1", "id": 1})

    updated_booking = await Booking.get(booking.id)
    assert updated_booking.status == BookingStatus.ESCROW_FUNDED
    assert updated_booking.escrow_status == EscrowStatus.HELD_IN_ESCROW

    updated_tx = await Transaction.get(tx.id)
    assert updated_tx.status == TransactionStatus.SUCCESSFUL


async def test_handle_charge_success_ignores_already_processed_transaction():
    client = await make_user("wc2@example.com")
    artisan = await make_user("wa2@example.com", role="artisan")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000,
        status=BookingStatus.ESCROW_FUNDED, escrow_status=EscrowStatus.HELD_IN_ESCROW,
    )
    await booking.insert()
    tx = Transaction(
        booking=booking, transaction_reference="KZ-ESCROW-test2", amount=5000,
        type=TransactionType.ESCROW_DEPOSIT, status=TransactionStatus.SUCCESSFUL,
    )
    await tx.insert()

    # Replaying charge.success for an already-successful transaction must be
    # a no-op (idempotency at the domain level, on top of the event-id dedup).
    await _handle_charge_success({"reference": "KZ-ESCROW-test2", "id": 2})

    unchanged = await Booking.get(booking.id)
    assert unchanged.status == BookingStatus.ESCROW_FUNDED


async def test_handle_transfer_success_marks_transaction_successful():
    client = await make_user("wc3@example.com")
    artisan = await make_user("wa3@example.com", role="artisan")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000,
        status=BookingStatus.PAID_OUT, escrow_status=EscrowStatus.RELEASED_TO_ARTISAN,
    )
    await booking.insert()
    tx = Transaction(
        booking=booking, transaction_reference="KZ-PAYOUT-test1", amount=4500,
        type=TransactionType.ESCROW_RELEASE, status=TransactionStatus.PENDING,
    )
    await tx.insert()

    await _handle_transfer_event("transfer.success", {"reference": "KZ-PAYOUT-test1"})

    updated_tx = await Transaction.get(tx.id)
    assert updated_tx.status == TransactionStatus.SUCCESSFUL


async def test_handle_transfer_failed_flags_ledger_notifies_and_logs():
    client = await make_user("wc4@example.com")
    artisan = await make_user("wa4@example.com", role="artisan")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000,
        status=BookingStatus.PAID_OUT, escrow_status=EscrowStatus.RELEASED_TO_ARTISAN,
    )
    await booking.insert()
    tx = Transaction(
        booking=booking, transaction_reference="KZ-PAYOUT-test2", amount=4500,
        type=TransactionType.ESCROW_RELEASE, status=TransactionStatus.PENDING,
    )
    await tx.insert()

    await _handle_transfer_event(
        "transfer.failed", {"reference": "KZ-PAYOUT-test2", "message": "Insufficient funds"}
    )

    updated_tx = await Transaction.get(tx.id)
    assert updated_tx.status == TransactionStatus.FAILED

    notifications = await Notification.find({"user.$id": artisan.id}).to_list()
    assert len(notifications) == 1
    assert notifications[0].type == "payout_issue"

    logs = await AuditLog.find({"target_type": "transaction", "target_id": str(tx.id)}).to_list()
    assert len(logs) == 1
    assert logs[0].action == "payout_failed"


async def test_handle_transfer_reversed_sets_reversed_status():
    client = await make_user("wc5@example.com")
    artisan = await make_user("wa5@example.com", role="artisan")
    booking = Booking(client=client, artisan=artisan, title="Job", amount=5000)
    await booking.insert()
    tx = Transaction(
        booking=booking, transaction_reference="KZ-PAYOUT-test3", amount=4500,
        type=TransactionType.ESCROW_RELEASE, status=TransactionStatus.SUCCESSFUL,
    )
    await tx.insert()

    await _handle_transfer_event("transfer.reversed", {"reference": "KZ-PAYOUT-test3"})

    updated_tx = await Transaction.get(tx.id)
    assert updated_tx.status == TransactionStatus.REVERSED
