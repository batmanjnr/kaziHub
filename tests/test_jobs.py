from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import get_password_hash
from app.models.bank_account import BankAccount
from app.models.booking import Booking, BookingStatus, EscrowStatus
from app.models.booking_status_history import BookingStatusHistory
from app.models.notification import Notification
from app.models.pending_user import PendingUser
from app.models.session import UserSession
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.models.webhook_event import ProcessedWebhookEvent
from app.services.jobs.auto_release import run_auto_release
from app.services.jobs.cleanup import cleanup_expired_otps_and_sessions
from app.services.jobs.notify import dispatch_pending_notifications
from app.services.jobs.webhook_retry import retry_failed_webhooks

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


# --- Auto-release cron ---

async def test_auto_release_completes_bookings_past_deadline():
    client = await make_user("jc1@example.com")
    artisan = await make_user("ja1@example.com", role="artisan")
    await give_verified_bank_account(artisan)

    past_due = Booking(
        client=client, artisan=artisan, title="Job", amount=5000, escrow_amount=5000,
        artisan_earnings=4500, status=BookingStatus.COMPLETED_BY_ARTISAN,
        escrow_status=EscrowStatus.HELD_IN_ESCROW,
        auto_completion_deadline=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await past_due.insert()

    not_due_yet = Booking(
        client=client, artisan=artisan, title="Job2", amount=3000, escrow_amount=3000,
        status=BookingStatus.COMPLETED_BY_ARTISAN, escrow_status=EscrowStatus.HELD_IN_ESCROW,
        auto_completion_deadline=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    await not_due_yet.insert()

    result = await run_auto_release()
    assert result["candidates"] == 1
    assert result["released"] == 1

    updated = await Booking.get(past_due.id)
    assert updated.status == BookingStatus.PAID_OUT
    assert updated.escrow_status == EscrowStatus.RELEASED_TO_ARTISAN

    unchanged = await Booking.get(not_due_yet.id)
    assert unchanged.status == BookingStatus.COMPLETED_BY_ARTISAN

    notifications = await Notification.find({"user.$id": client.id}).to_list()
    assert len(notifications) == 1
    assert notifications[0].type == "escrow_auto_released"

    history = await BookingStatusHistory.find({"booking.$id": past_due.id}).to_list()
    assert any(h.to_status == "paid_out" for h in history)


async def test_auto_release_skips_a_booking_already_disputed():
    """The exact race the spec is worried about: a dispute wins before the
    cron gets to a booking. The cron must skip it, not error or override."""
    client = await make_user("jc2@example.com")
    artisan = await make_user("ja2@example.com", role="artisan")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000,
        status=BookingStatus.DISPUTED,  # already moved on by a dispute
        escrow_status=EscrowStatus.HELD_IN_ESCROW,
        auto_completion_deadline=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await booking.insert()

    # This booking won't even be a candidate since the query filters on
    # status == completed_by_artisan — confirms the cron's query itself
    # excludes disputed bookings per spec §6's dispute-path note.
    result = await run_auto_release()
    assert result["candidates"] == 0

    unchanged = await Booking.get(booking.id)
    assert unchanged.status == BookingStatus.DISPUTED


async def test_auto_release_leaves_booking_alone_if_payout_cannot_be_initiated():
    client = await make_user("jc3@example.com")
    artisan = await make_user("ja3@example.com", role="artisan")
    # No verified bank account on file.
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000, escrow_amount=5000,
        status=BookingStatus.COMPLETED_BY_ARTISAN, escrow_status=EscrowStatus.HELD_IN_ESCROW,
        auto_completion_deadline=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await booking.insert()

    result = await run_auto_release()
    assert result["failed_payout"] == 1
    assert result["released"] == 0

    unchanged = await Booking.get(booking.id)
    assert unchanged.status == BookingStatus.COMPLETED_BY_ARTISAN
    assert unchanged.escrow_status == EscrowStatus.HELD_IN_ESCROW


# --- OTP / token cleanup ---

async def test_cleanup_removes_expired_pending_registrations_and_stale_sessions():
    expired_pending = PendingUser(
        first_name="X", last_name="Y", email="expired@example.com", phone_number="+2348000000000",
        state="Lagos", role="client", hashed_password="hash",
        otp_code="11111", otp_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        created_at=datetime.now(timezone.utc),
    )
    await expired_pending.insert()

    fresh_pending = PendingUser(
        first_name="X", last_name="Y", email="fresh@example.com", phone_number="+2348000000001",
        state="Lagos", role="client", hashed_password="hash",
        otp_code="22222", otp_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        created_at=datetime.now(timezone.utc),
    )
    await fresh_pending.insert()

    user = await make_user("cleanup1@example.com")
    stale_session = UserSession(
        user=user, refresh_token_hash="abc", family_id="fam1",
        expires_at=datetime.now(timezone.utc) - timedelta(days=30),
        created_at=datetime.now(timezone.utc) - timedelta(days=90),
    )
    await stale_session.insert()
    recent_session = UserSession(
        user=user, refresh_token_hash="def", family_id="fam2",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        created_at=datetime.now(timezone.utc),
    )
    await recent_session.insert()

    user.reset_otp_code = "99999"
    user.reset_otp_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await user.save()

    result = await cleanup_expired_otps_and_sessions()

    remaining_pending = await PendingUser.find_all().to_list()
    assert len(remaining_pending) == 1
    assert remaining_pending[0].email == "fresh@example.com"

    remaining_sessions = await UserSession.find_all().to_list()
    assert len(remaining_sessions) == 1
    assert remaining_sessions[0].family_id == "fam2"

    refreshed_user = await User.get(user.id)
    assert refreshed_user.reset_otp_code is None


# --- SMS/WhatsApp dispatch ---

async def test_dispatch_pending_notifications_marks_sent():
    user = await make_user("notif1@example.com")
    n = Notification(
        user=user, type="booking_update", title="Update", message="Your job is scheduled.",
        channel="sms", delivery_status="pending",
    )
    await n.insert()

    result = await dispatch_pending_notifications()
    assert result["sent"] == 1
    assert result["failed"] == 0

    refreshed = await Notification.get(n.id)
    assert refreshed.delivery_status == "sent"


async def test_dispatch_pending_notifications_ignores_in_app_channel():
    user = await make_user("notif2@example.com")
    n = Notification(
        user=user, type="booking_update", title="Update", message="hi",
        channel="in_app", delivery_status="pending",
    )
    await n.insert()

    result = await dispatch_pending_notifications()
    assert result["total"] == 0

    refreshed = await Notification.get(n.id)
    assert refreshed.delivery_status == "pending"


# --- Webhook retry processor ---

async def test_webhook_retry_completes_a_previously_failed_event():
    client = await make_user("wrc1@example.com")
    artisan = await make_user("wra1@example.com", role="artisan")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000,
        status=BookingStatus.ACCEPTED, escrow_status=EscrowStatus.UNFUNDED,
    )
    await booking.insert()
    tx = Transaction(
        booking=booking, transaction_reference="KZ-ESCROW-retry1", amount=5000,
        type=TransactionType.ESCROW_DEPOSIT, status=TransactionStatus.PENDING,
    )
    await tx.insert()

    record = ProcessedWebhookEvent(
        gateway="paystack", event_id="charge.success:99",
        event_type="charge.success", status="failed_pending_retry",
        payload={"event": "charge.success", "data": {"reference": "KZ-ESCROW-retry1", "id": 99}},
        attempts=1,
        next_retry_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        last_error="transient DB error",
    )
    await record.insert()

    result = await retry_failed_webhooks()
    assert result["completed"] == 1

    updated_booking = await Booking.get(booking.id)
    assert updated_booking.status == BookingStatus.ESCROW_FUNDED

    refreshed_record = await ProcessedWebhookEvent.get(record.id)
    assert refreshed_record.status == "completed"


async def test_webhook_retry_ignores_events_not_yet_due():
    record = ProcessedWebhookEvent(
        gateway="paystack", event_id="charge.success:100",
        event_type="charge.success", status="failed_pending_retry",
        payload={"event": "charge.success", "data": {"reference": "nonexistent"}},
        next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    await record.insert()

    result = await retry_failed_webhooks()
    assert result["due"] == 0


async def test_webhook_retry_stops_after_max_attempts():
    from app.services.jobs.webhook_retry import MAX_ATTEMPTS

    record = ProcessedWebhookEvent(
        gateway="paystack", event_id="charge.success:101",
        event_type="charge.success", status="failed_pending_retry",
        payload={"event": "charge.success", "data": {"reference": "nonexistent"}},
        attempts=MAX_ATTEMPTS,
        next_retry_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await record.insert()

    result = await retry_failed_webhooks()
    assert result["exhausted"] == 1
    assert result["completed"] == 0
