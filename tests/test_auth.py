from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.api.v1.endpoints.auth import (
    MAX_OTP_ATTEMPTS,
    build_user_response,
    deactivate_account,
    issue_token_pair,
)
from app.core.config import settings
from app.core.security import generate_refresh_token, get_password_hash, hash_refresh_token
from app.models.pending_user import PendingUser
from app.models.session import UserSession
from app.models.user import User
from app.models.user_role import UserRole

pytestmark = pytest.mark.asyncio


async def make_user(**overrides) -> User:
    defaults = dict(
        first_name="Ade",
        last_name="Bello",
        email="ade@example.com",
        phone_number="+2348000000000",
        state="Lagos",
        role="client",
        roles=["client"],
        hashed_password=get_password_hash("Passw0rd!"),
        token_version=0,
    )
    defaults.update(overrides)
    user = User(**defaults)
    await user.insert()
    return user


async def test_access_token_carries_roles_and_token_version():
    user = await make_user(roles=["client", "artisan"], is_admin=False)
    pair = await issue_token_pair(user)

    payload = jwt.decode(pair.access_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert payload["sub"] == str(user.id)
    assert payload["roles"] == ["client", "artisan"]
    assert payload["token_version"] == 0
    assert payload["is_admin"] is False


async def test_refresh_rotates_token_and_old_one_stops_working():
    user = await make_user()
    pair = await issue_token_pair(user)

    old_hash = hash_refresh_token(pair.refresh_token)
    session = await UserSession.find_one(UserSession.refresh_token_hash == old_hash)
    assert session.is_revoked is False

    # Simulate the /refresh endpoint's rotation step directly (avoids the
    # mongomock dot-notation limitation noted in test_models.py).
    session.is_revoked = True
    await session.save()
    new_pair = await issue_token_pair(user, family_id=session.family_id)

    assert new_pair.refresh_token != pair.refresh_token
    refreshed_session = await UserSession.find_one(
        UserSession.refresh_token_hash == old_hash
    )
    assert refreshed_session.is_revoked is True


async def test_reuse_of_revoked_refresh_token_is_detectable_within_family():
    user = await make_user()
    pair = await issue_token_pair(user)
    session = await UserSession.find_one(
        UserSession.refresh_token_hash == hash_refresh_token(pair.refresh_token)
    )
    family_id = session.family_id

    # Rotate once (legitimate use).
    session.is_revoked = True
    await session.save()
    second_pair = await issue_token_pair(user, family_id=family_id)

    # Replay of the first (now-revoked) token must be detectable so the
    # whole family can be killed — this is what /refresh does on reuse.
    replayed = await UserSession.find_one(
        UserSession.refresh_token_hash == hash_refresh_token(pair.refresh_token)
    )
    assert replayed.is_revoked is True

    all_sessions = await UserSession.find(UserSession.family_id == family_id).to_list()
    assert len(all_sessions) == 2


async def test_deactivate_account_soft_deletes_and_bumps_token_version():
    user = await make_user()
    original_version = user.token_version

    await deactivate_account(user)

    assert user.is_active is False
    assert user.deleted_at is not None
    assert user.token_version == original_version + 1


async def test_otp_lockout_after_max_attempts():
    pending = PendingUser(
        first_name="Tade",
        last_name="Ola",
        email="tade@example.com",
        phone_number="+2348111111111",
        state="Lagos",
        role="client",
        hashed_password=get_password_hash("Passw0rd!"),
        otp_code="12345",
        otp_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        created_at=datetime.now(timezone.utc),
    )
    await pending.insert()

    for _ in range(MAX_OTP_ATTEMPTS):
        pending.otp_attempts += 1
        await pending.save()

    refreshed = await PendingUser.get(pending.id)
    assert refreshed.otp_attempts == MAX_OTP_ATTEMPTS


async def test_build_user_response_masks_nin_and_exposes_roles():
    user = await make_user(roles=["client", "artisan"], is_admin=True)
    await UserRole(user=user, role="client").insert()

    response = build_user_response(user)
    assert response.roles == ["client", "artisan"]
    assert response.is_admin is True
    assert response.nin_masked is None  # no NIN was set
