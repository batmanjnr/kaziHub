import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.auth import (
    change_password,
    confirm_email_change,
    freeze_me,
    issue_token_pair,
    list_sessions,
    request_email_change,
    revoke_session,
    unfreeze_me,
)
from app.core.security import get_password_hash, verify_password
from app.models.profile import Profile
from app.models.session import UserSession
from app.models.user import User
from app.schemas.auth import ChangePasswordSchema, ConfirmEmailChangeSchema, RequestEmailChangeSchema

pytestmark = pytest.mark.asyncio


class FakeBackgroundTasks:
    def add_task(self, func, *args, **kwargs):
        func(*args, **kwargs)


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


async def test_change_password_requires_correct_current_password():
    user = await make_user()
    with pytest.raises(HTTPException) as exc:
        await change_password(
            ChangePasswordSchema(current_password="wrong", new_password="NewPassw0rd!"),
            current_user=user,
        )
    assert exc.value.status_code == 400


async def test_change_password_updates_hash_and_revokes_sessions():
    user = await make_user()
    await issue_token_pair(user)
    old_version = user.token_version

    await change_password(
        ChangePasswordSchema(current_password="Passw0rd!", new_password="NewPassw0rd!"),
        current_user=user,
    )

    assert verify_password("NewPassw0rd!", user.hashed_password)
    assert user.token_version == old_version + 1
    sessions = await UserSession.find({"user.$id": user.id}).to_list()
    assert all(s.is_revoked for s in sessions)


async def test_sessions_list_and_revoke():
    user = await make_user()
    pair = await issue_token_pair(user)

    sessions = await list_sessions(current_user=user)
    assert len(sessions) == 1

    await revoke_session(sessions[0].id, current_user=user)
    sessions_after = await list_sessions(current_user=user)
    assert sessions_after == []


async def test_revoke_session_rejects_other_users_session():
    owner = await make_user(email="owner@example.com")
    other = await make_user(email="other@example.com")
    await issue_token_pair(owner)
    sessions = await list_sessions(current_user=owner)

    with pytest.raises(HTTPException) as exc:
        await revoke_session(sessions[0].id, current_user=other)
    assert exc.value.status_code == 404


async def test_freeze_and_unfreeze_syncs_profile_and_allows_login():
    user = await make_user(role="artisan", roles=["artisan"], email="pro@example.com")
    profile = Profile(user=user, category="Plumbing", state="Lagos")
    await profile.insert()

    await freeze_me(current_user=user)
    assert user.is_paused is True
    refreshed_profile = await Profile.get(profile.id)
    assert refreshed_profile.is_paused is True
    # Freezing must not block login the way admin suspend does.
    assert user.is_active is True

    await unfreeze_me(current_user=user)
    assert user.is_paused is False
    refreshed_profile = await Profile.get(profile.id)
    assert refreshed_profile.is_paused is False


async def test_email_change_requires_correct_password():
    user = await make_user()
    with pytest.raises(HTTPException) as exc:
        await request_email_change(
            RequestEmailChangeSchema(new_email="new@example.com", current_password="wrong"),
            background_tasks=FakeBackgroundTasks(),
            current_user=user,
        )
    assert exc.value.status_code == 400


async def test_email_change_full_flow():
    user = await make_user()
    await request_email_change(
        RequestEmailChangeSchema(new_email="new@example.com", current_password="Passw0rd!"),
        background_tasks=FakeBackgroundTasks(),
        current_user=user,
    )
    assert user.pending_email == "new@example.com"
    otp = user.email_change_otp

    response = await confirm_email_change(ConfirmEmailChangeSchema(otp=otp), current_user=user)
    assert response.email == "new@example.com"
    assert user.email == "new@example.com"
    assert user.pending_email is None


async def test_email_change_rejects_wrong_otp():
    user = await make_user()
    await request_email_change(
        RequestEmailChangeSchema(new_email="new@example.com", current_password="Passw0rd!"),
        background_tasks=FakeBackgroundTasks(),
        current_user=user,
    )
    with pytest.raises(HTTPException) as exc:
        await confirm_email_change(ConfirmEmailChangeSchema(otp="00000"), current_user=user)
    assert exc.value.status_code == 400
    assert user.email == "ade@example.com"
