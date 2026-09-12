from datetime import datetime, timezone

import pyotp
import pytest
from fastapi import HTTPException

from app.api.deps import get_current_admin
from app.api.v1.endpoints.admin import (
    ForceRefundRequest,
    ForceReleaseRequest,
    ReactivateUserRequest,
    SuspendUserRequest,
    analytics_overview,
    force_refund_booking,
    force_release_booking,
    list_audit_logs,
    reactivate_user,
    resolve_dispute,
    suspend_user,
)
from app.api.v1.endpoints.admin import DisputeResolveRequest
from app.api.v1.endpoints.auth import TwoFactorVerifySchema, setup_two_factor, verify_two_factor_setup
from app.core.security import get_password_hash
from app.models.audit_log import AuditLog
from app.models.bank_account import BankAccount
from app.models.booking import Booking, BookingStatus, EscrowStatus
from app.models.dispute import Dispute
from app.models.session import UserSession
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def make_user(email, is_admin=False, role="client") -> User:
    user = User(
        first_name="A",
        last_name="B",
        email=email,
        phone_number=f"+234800000{abs(hash(email)) % 10000:04d}",
        state="Lagos",
        role=role,
        hashed_password=get_password_hash("Passw0rd!"),
        is_admin=is_admin,
    )
    await user.insert()
    return user


async def make_admin_with_2fa(email) -> tuple:
    admin = await make_user(email, is_admin=True)
    setup = await setup_two_factor(current_user=admin)
    totp = pyotp.TOTP(setup.secret)
    await verify_two_factor_setup(
        TwoFactorVerifySchema(totp_code=totp.now()), current_user=admin
    )
    refreshed = await User.get(admin.id)
    return refreshed, totp


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


async def make_disputed_booking(client, artisan, amount=10000) -> Booking:
    booking = Booking(
        client=client,
        artisan=artisan,
        title="Fix sink",
        amount=amount,
        escrow_amount=amount,
        status=BookingStatus.DISPUTED,
        escrow_status=EscrowStatus.HELD_IN_ESCROW,
    )
    await booking.insert()
    return booking


async def test_admin_without_2fa_is_rejected_by_dependency():
    admin_no_2fa = await make_user("admin_no2fa@example.com", is_admin=True)
    with pytest.raises(HTTPException) as exc_info:
        await get_current_admin(current_user=admin_no_2fa)
    assert exc_info.value.status_code == 403
    assert "2FA" in exc_info.value.detail


async def test_2fa_setup_and_verify_flow():
    admin, totp = await make_admin_with_2fa("admin_2fa@example.com")
    assert admin.two_factor_enabled is True

    # Now the dependency accepts them.
    resolved = await get_current_admin(current_user=admin)
    assert resolved.id == admin.id


async def test_resolve_dispute_client_refund_creates_ledger_and_audit_log():
    client = await make_user("dc1@example.com")
    artisan = await make_user("da1@example.com", role="artisan")
    admin, totp = await make_admin_with_2fa("admin_d1@example.com")
    booking = await make_disputed_booking(client, artisan)
    dispute = Dispute(
        ticket_id="DSP-TEST1",
        booking=booking,
        client=client,
        artisan=artisan,
        reason="not_completed",
        details="Never finished",
    )
    await dispute.insert()

    result = await resolve_dispute(
        str(dispute.id),
        DisputeResolveRequest(resolution="client_refund", totp_code=totp.now()),
        current_admin=admin,
        idempotency_key="dispute-resolve-1",
    )
    assert result.status == "resolved_client_refund"

    updated_booking = await Booking.get(booking.id)
    assert updated_booking.status == BookingStatus.CANCELLED
    assert updated_booking.escrow_status == EscrowStatus.REFUNDED_TO_CLIENT

    ledger = await Transaction.find({"booking.$id": booking.id}).to_list()
    assert len(ledger) == 1
    assert ledger[0].amount == 10000

    logs = await AuditLog.find({"target_type": "dispute", "target_id": str(dispute.id)}).to_list()
    assert len(logs) == 1
    assert logs[0].action == "dispute_resolved"


async def test_resolve_dispute_rejects_wrong_totp_code():
    client = await make_user("dc2@example.com")
    artisan = await make_user("da2@example.com", role="artisan")
    admin, _ = await make_admin_with_2fa("admin_d2@example.com")
    booking = await make_disputed_booking(client, artisan)
    dispute = Dispute(
        ticket_id="DSP-TEST2", booking=booking, client=client, artisan=artisan,
        reason="quality", details="Bad work",
    )
    await dispute.insert()

    with pytest.raises(HTTPException) as exc_info:
        await resolve_dispute(
            str(dispute.id),
            DisputeResolveRequest(resolution="client_refund", totp_code="000000"),
            current_admin=admin,
            idempotency_key="dispute-resolve-bad",
        )
    assert exc_info.value.status_code == 401


async def test_resolve_dispute_split_divides_funds_by_ratio():
    client = await make_user("dc3@example.com")
    artisan = await make_user("da3@example.com", role="artisan")
    await give_verified_bank_account(artisan)
    admin, totp = await make_admin_with_2fa("admin_d3@example.com")
    booking = await make_disputed_booking(client, artisan, amount=10000)
    dispute = Dispute(
        ticket_id="DSP-TEST3", booking=booking, client=client, artisan=artisan,
        reason="partial", details="Half finished",
    )
    await dispute.insert()

    await resolve_dispute(
        str(dispute.id),
        DisputeResolveRequest(resolution="split", split_ratio=0.6, totp_code=totp.now()),
        current_admin=admin,
        idempotency_key="dispute-resolve-split",
    )

    ledger = await Transaction.find({"booking.$id": booking.id}).to_list()
    amounts = sorted(tx.amount for tx in ledger)
    assert amounts == [4000.0, 6000.0]


async def test_force_release_requires_held_escrow():
    client = await make_user("fc1@example.com")
    artisan = await make_user("fa1@example.com", role="artisan")
    admin, totp = await make_admin_with_2fa("admin_f1@example.com")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000,
        status=BookingStatus.PENDING, escrow_status=EscrowStatus.UNFUNDED,
    )
    await booking.insert()

    with pytest.raises(HTTPException) as exc_info:
        await force_release_booking(
            str(booking.id),
            ForceReleaseRequest(reason="test", totp_code=totp.now()),
            current_admin=admin,
            idempotency_key="force-release-1",
        )
    assert exc_info.value.status_code == 400


async def test_force_release_without_verified_bank_account_fails_cleanly():
    client = await make_user("fc3@example.com")
    artisan = await make_user("fa3@example.com", role="artisan")
    admin, totp = await make_admin_with_2fa("admin_f3@example.com")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000, escrow_amount=5000,
        status=BookingStatus.IN_PROGRESS, escrow_status=EscrowStatus.HELD_IN_ESCROW,
    )
    await booking.insert()

    with pytest.raises(HTTPException) as exc_info:
        await force_release_booking(
            str(booking.id),
            ForceReleaseRequest(reason="test", totp_code=totp.now()),
            current_admin=admin,
            idempotency_key="force-release-no-bank",
        )
    assert exc_info.value.status_code == 400
    assert "bank account" in exc_info.value.detail.lower()

    # No booking mutation and no ledger entry on a failed payout attempt.
    unchanged = await Booking.get(booking.id)
    assert unchanged.status == BookingStatus.IN_PROGRESS
    assert unchanged.escrow_status == EscrowStatus.HELD_IN_ESCROW
    ledger = await Transaction.find({"booking.$id": booking.id}).to_list()
    assert ledger == []


async def test_force_release_with_verified_bank_account_succeeds():
    client = await make_user("fc4@example.com")
    artisan = await make_user("fa4@example.com", role="artisan")
    await give_verified_bank_account(artisan)
    admin, totp = await make_admin_with_2fa("admin_f4@example.com")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=5000, escrow_amount=5000,
        status=BookingStatus.IN_PROGRESS, escrow_status=EscrowStatus.HELD_IN_ESCROW,
    )
    await booking.insert()

    updated = await force_release_booking(
        str(booking.id),
        ForceReleaseRequest(reason="test", totp_code=totp.now()),
        current_admin=admin,
        idempotency_key="force-release-with-bank",
    )
    assert updated.status == BookingStatus.PAID_OUT
    assert updated.escrow_status == EscrowStatus.RELEASED_TO_ARTISAN

    ledger = await Transaction.find({"booking.$id": booking.id}).to_list()
    assert len(ledger) == 1
    assert ledger[0].status == TransactionStatus.PENDING


async def test_force_refund_partial_keeps_escrow_partially_refunded():
    client = await make_user("fc2@example.com")
    artisan = await make_user("fa2@example.com", role="artisan")
    admin, totp = await make_admin_with_2fa("admin_f2@example.com")
    booking = Booking(
        client=client, artisan=artisan, title="Job", amount=10000, escrow_amount=10000,
        status=BookingStatus.IN_PROGRESS, escrow_status=EscrowStatus.HELD_IN_ESCROW,
    )
    await booking.insert()

    updated = await force_refund_booking(
        str(booking.id),
        ForceRefundRequest(amount=4000, reason="partial goodwill refund", totp_code=totp.now()),
        current_admin=admin,
        idempotency_key="force-refund-partial",
    )
    assert updated.escrow_status == EscrowStatus.PARTIALLY_REFUNDED
    assert updated.status == BookingStatus.IN_PROGRESS  # unchanged, not a full refund


async def test_suspend_user_bumps_token_version_and_revokes_sessions():
    admin, totp = await make_admin_with_2fa("admin_s1@example.com")
    target = await make_user("suspend_target@example.com")
    session = UserSession(
        user=target, refresh_token_hash="abc", family_id="fam1",
        expires_at=datetime.now(timezone.utc),
    )
    await session.insert()

    await suspend_user(
        str(target.id), SuspendUserRequest(reason="fraud", totp_code=totp.now()), current_admin=admin
    )

    refreshed = await User.get(target.id)
    assert refreshed.is_active is False
    assert refreshed.is_frozen is True
    assert refreshed.token_version == 1

    refreshed_session = await UserSession.get(session.id)
    assert refreshed_session.is_revoked is True

    logs = await AuditLog.find({"target_type": "user", "target_id": str(target.id)}).to_list()
    assert logs[0].action == "user_suspended"


async def test_cannot_suspend_an_admin_account():
    admin, totp = await make_admin_with_2fa("admin_s2@example.com")
    other_admin = await make_user("other_admin@example.com", is_admin=True)

    with pytest.raises(HTTPException) as exc_info:
        await suspend_user(
            str(other_admin.id),
            SuspendUserRequest(reason="test", totp_code=totp.now()),
            current_admin=admin,
        )
    assert exc_info.value.status_code == 400


async def test_reactivate_user_restores_access():
    admin, totp = await make_admin_with_2fa("admin_s3@example.com")
    target = await make_user("reactivate_target@example.com")
    target.is_active = False
    target.is_frozen = True
    await target.save()

    await reactivate_user(
        str(target.id), ReactivateUserRequest(reason=None, totp_code=totp.now()), current_admin=admin
    )

    refreshed = await User.get(target.id)
    assert refreshed.is_active is True
    assert refreshed.is_frozen is False


async def test_analytics_overview_reports_disputes_and_kyc_queue():
    admin, _ = await make_admin_with_2fa("admin_an1@example.com")
    client = await make_user("anc1@example.com")
    artisan = await make_user("ana1@example.com", role="artisan")
    booking = await make_disputed_booking(client, artisan)
    await Dispute(
        ticket_id="DSP-AN1", booking=booking, client=client, artisan=artisan,
        reason="x", details="y",
    ).insert()

    overview = await analytics_overview(current_admin=admin)
    assert overview["active_disputes"] == 1
    assert "kyc_queue_depth" in overview


async def test_audit_log_filtering_by_action():
    admin, totp = await make_admin_with_2fa("admin_al1@example.com")
    target = await make_user("audit_target@example.com")

    await suspend_user(
        str(target.id), SuspendUserRequest(reason="test", totp_code=totp.now()), current_admin=admin
    )
    await reactivate_user(
        str(target.id), ReactivateUserRequest(reason=None, totp_code=totp.now()), current_admin=admin
    )

    logs = await list_audit_logs(
        actor_id=str(admin.id), target_type=None, action="user_suspended",
        date_from=None, date_to=None, limit=20, offset=0, current_admin=admin,
    )
    assert len(logs) == 1
    assert logs[0].action == "user_suspended"
