import pytest

from app.api.v1.endpoints.payments import VerifyBankAccountRequest, get_my_bank_account, verify_bank_account
from app.core.security import get_password_hash
from app.models.bank_account import BankAccount
from app.models.user import User


async def make_artisan(email) -> User:
    user = User(
        first_name="Ade",
        last_name="Bello",
        email=email,
        phone_number=f"+234800000{abs(hash(email)) % 10000:04d}",
        state="Lagos",
        role="artisan",
        hashed_password=get_password_hash("Passw0rd!"),
    )
    await user.insert()
    return user


async def test_verify_bank_account_resolves_and_registers_recipient(monkeypatch):
    async def fake_resolve(account_number, bank_code):
        return {"account_name": "ADE BELLO", "bank_name": "Test Bank"}

    async def fake_create_recipient(name, account_number, bank_code):
        return {"recipient_code": "RCP_abc123"}

    monkeypatch.setattr("app.api.v1.endpoints.payments.resolve_account_number", fake_resolve)
    monkeypatch.setattr(
        "app.api.v1.endpoints.payments.create_transfer_recipient", fake_create_recipient
    )

    artisan = await make_artisan("pay1@example.com")
    result = await verify_bank_account(
        VerifyBankAccountRequest(bank_code="058", account_number="0123456789"),
        current_artisan=artisan,
    )

    assert result.account_name == "ADE BELLO"
    assert result.is_verified is True

    stored = await BankAccount.find_one({"user.$id": artisan.id})
    assert stored.paystack_recipient_code == "RCP_abc123"

    fetched = await get_my_bank_account(current_artisan=artisan)
    assert fetched.bank_code == "058"


async def test_verify_bank_account_updates_existing_record(monkeypatch):
    async def fake_resolve(account_number, bank_code):
        return {"account_name": "ADE BELLO", "bank_name": "Test Bank"}

    async def fake_create_recipient(name, account_number, bank_code):
        return {"recipient_code": "RCP_new"}

    monkeypatch.setattr("app.api.v1.endpoints.payments.resolve_account_number", fake_resolve)
    monkeypatch.setattr(
        "app.api.v1.endpoints.payments.create_transfer_recipient", fake_create_recipient
    )

    artisan = await make_artisan("pay2@example.com")
    await BankAccount(
        user=artisan, bank_code="011", bank_name="Old Bank", account_number="000",
        account_name="OLD NAME", paystack_recipient_code="RCP_old", is_verified=True,
    ).insert()

    await verify_bank_account(
        VerifyBankAccountRequest(bank_code="058", account_number="0123456789"),
        current_artisan=artisan,
    )

    accounts = await BankAccount.find({"user.$id": artisan.id}).to_list()
    assert len(accounts) == 1
    assert accounts[0].paystack_recipient_code == "RCP_new"
    assert accounts[0].bank_code == "058"
