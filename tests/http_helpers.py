"""Helpers for tests that go through the real HTTP stack (routing,
dependencies, validation, middleware) instead of calling endpoint
functions directly."""
import uuid

import httpx

from app.api.v1.endpoints.auth import issue_token_pair
from app.core.security import get_password_hash
from app.main import app
from app.models.bank_account import BankAccount
from app.models.profile import Profile
from app.models.user import User

API = "/api/v1"


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def make_user(email: str, role: str = "client", **extra) -> User:
    user = User(
        first_name=extra.pop("first_name", "Ada"),
        last_name=extra.pop("last_name", "Obi"),
        email=email,
        phone_number=extra.pop("phone_number", f"+23480{abs(hash(email)) % 10**8:08d}"),
        state=extra.pop("state", "Lagos"),
        role=role,
        roles=["client", "artisan"] if role == "artisan" else ["client"],
        hashed_password=get_password_hash("Passw0rd!"),
        is_email_verified=True,
        **extra,
    )
    await user.insert()
    return user


async def make_artisan(email: str, category: str = "Plumbers", **profile_extra) -> tuple:
    user = await make_user(email, role="artisan", first_name="Bola", last_name="Ade")
    profile = Profile(user=user, category=category, state="Lagos", **profile_extra)
    await profile.insert()
    return user, profile


async def give_bank_account(user: User) -> None:
    await BankAccount(
        user=user, bank_code="058", bank_name="Test Bank", account_number="0123456789",
        account_name="Bola Ade", paystack_recipient_code="RCP_test", is_verified=True,
    ).insert()


async def auth_headers(user: User, idempotent: bool = False) -> dict:
    pair = await issue_token_pair(user)
    headers = {"Authorization": f"Bearer {pair.access_token}"}
    if idempotent:
        headers["Idempotency-Key"] = str(uuid.uuid4())
    return headers


async def login_pair(user: User):
    return await issue_token_pair(user)
