import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.v1.endpoints.auth import register, verify_email
from app.core.nin_hash import compute_nin_hash
from app.core.security import get_password_hash
from app.models.pending_user import PendingUser
from app.models.user import User, UserCreate, UserUpdate, VerifyEmailSchema

# pytest.ini sets asyncio_mode = auto; the one sync test below is unmarked.


class FakeClient:
    host = "203.0.113.5"


class FakeRequest:
    client = FakeClient()


def test_compute_nin_hash_is_deterministic_and_not_reversible():
    h1 = compute_nin_hash("12345678901")
    h2 = compute_nin_hash("12345678901")
    h3 = compute_nin_hash("10987654321")
    assert h1 == h2  # same NIN -> same hash, every time
    assert h1 != h3  # different NIN -> different hash
    assert "12345678901" not in h1  # not reversible / doesn't leak the NIN


async def _register_and_verify(email: str, nin: str, phone: str):
    await register(
        FakeRequest(),
        UserCreate(
            first_name="A", last_name="B", email=email, password="Passw0rd!",
            phone_number=phone, nin=nin, state="Lagos", role="client",
        ),
        BackgroundTasks(),
    )
    pending = await PendingUser.find_one(PendingUser.email == email)
    return await verify_email(VerifyEmailSchema(email=email, otp=pending.otp_code))


async def test_second_registration_with_same_nin_is_rejected_at_register():
    await _register_and_verify("ninowner@example.com", "11122233344", "+2348111111111")

    with pytest.raises(HTTPException) as exc_info:
        await register(
            FakeRequest(),
            UserCreate(
                first_name="C", last_name="D", email="ninattacker@example.com",
                password="Passw0rd!", phone_number="+2348222222222",
                nin="11122233344", state="Lagos", role="client",
            ),
            BackgroundTasks(),
        )
    assert exc_info.value.status_code == 400
    assert "already exists" in exc_info.value.detail.lower()


async def test_registration_without_nin_is_unaffected():
    """The exact scenario that broke on the stale plaintext-nin unique
    index: multiple accounts with no NIN at all must all succeed, since
    the new index is sparse."""
    await _register_and_verify("noNin1@example.com", None, "+2348333333333")
    await _register_and_verify("noNin2@example.com", None, "+2348444444444")

    users = await User.find({"email": {"$in": ["noNin1@example.com", "noNin2@example.com"]}}).to_list()
    assert len(users) == 2
    assert all(u.nin_hash is None for u in users)


async def test_updating_nin_to_one_already_claimed_is_rejected():
    from app.api.v1.endpoints.auth import update_user_me

    await _register_and_verify("ninholder@example.com", "99988877766", "+2348555555555")
    other_user = User(
        first_name="E", last_name="F", email="otherperson@example.com",
        phone_number="+2348666666666", state="Lagos", role="client",
        hashed_password=get_password_hash("Passw0rd!"),
    )
    await other_user.insert()

    with pytest.raises(HTTPException) as exc_info:
        await update_user_me(UserUpdate(nin="99988877766"), current_user=other_user)
    assert exc_info.value.status_code == 400


async def test_updating_own_nin_to_the_same_value_is_a_no_op_not_an_error():
    from app.api.v1.endpoints.auth import update_user_me

    user_response = await _register_and_verify("selfnin@example.com", "55566677788", "+2348777777777")
    user = await User.get(user_response.id)

    result = await update_user_me(UserUpdate(nin="55566677788"), current_user=user)
    assert result.id == str(user.id)
