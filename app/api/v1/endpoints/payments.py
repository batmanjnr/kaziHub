# app/api/v1/endpoints/payments.py
import time
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import get_current_artisan, get_current_user
from app.models.bank_account import BankAccount, BankAccountResponse
from app.models.user import User
from app.services.paystack import PaystackError, create_transfer_recipient, list_banks, resolve_account_number

router = APIRouter()


class VerifyBankAccountRequest(BaseModel):
    bank_code: str
    account_number: str


class BankOption(BaseModel):
    code: str
    name: str


BANKS_CACHE_SECONDS = 24 * 60 * 60
_banks_cache: dict = {"at": 0.0, "banks": []}


@router.get("/banks", response_model=List[BankOption])
async def get_banks(current_user: User = Depends(get_current_user)):
    """Banks an artisan can be paid into, A-Z (ask 21). Send the chosen
    `code` as `bank_code` to POST /payments/verify-bank-account. Sourced
    from Paystack and cached for 24 hours."""
    if not _banks_cache["banks"] or time.time() - _banks_cache["at"] > BANKS_CACHE_SECONDS:
        try:
            raw = await list_banks()
        except PaystackError as e:
            if _banks_cache["banks"]:
                return _banks_cache["banks"]
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=e.client_message())
        seen, banks = set(), []
        for b in raw:
            code, name = b.get("code"), b.get("name")
            if not code or not name or not b.get("active", True) or b.get("is_deleted"):
                continue
            if (code, name) in seen:
                continue
            seen.add((code, name))
            banks.append(BankOption(code=code, name=name))
        _banks_cache["banks"] = sorted(banks, key=lambda x: x.name.lower())
        _banks_cache["at"] = time.time()
    return _banks_cache["banks"]


def build_bank_account_response(b: BankAccount) -> BankAccountResponse:
    return BankAccountResponse(
        id=str(b.id),
        bank_code=b.bank_code,
        bank_name=b.bank_name,
        account_number=b.account_number,
        account_name=b.account_name,
        is_verified=b.is_verified,
        created_at=b.created_at,
    )


@router.post("/verify-bank-account", response_model=BankAccountResponse)
async def verify_bank_account(
    payload: VerifyBankAccountRequest,
    current_artisan: User = Depends(get_current_artisan),
):
    """Resolves the account via Paystack NUBAN (spec §11.5), then registers
    a Paystack transfer recipient so payouts (§11.4) can actually be sent to
    it later. Nothing is saved unless both Paystack calls succeed."""
    try:
        resolved = await resolve_account_number(payload.account_number, payload.bank_code)
    except PaystackError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"Could not verify bank account: {e.client_message()}"
        )

    account_name = resolved.get("account_name", "")

    try:
        recipient = await create_transfer_recipient(
            account_name, payload.account_number, payload.bank_code
        )
    except PaystackError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"Could not register payout recipient: {e.client_message()}"
        )

    existing = await BankAccount.find_one({"user.$id": current_artisan.id})
    if existing:
        await existing.set(
            {
                "bank_code": payload.bank_code,
                "account_number": payload.account_number,
                "account_name": account_name,
                "paystack_recipient_code": recipient.get("recipient_code"),
                "is_verified": True,
            }
        )
        return build_bank_account_response(existing)

    bank_account = BankAccount(
        user=current_artisan,
        bank_code=payload.bank_code,
        bank_name=resolved.get("bank_name", ""),
        account_number=payload.account_number,
        account_name=account_name,
        paystack_recipient_code=recipient.get("recipient_code"),
        is_verified=True,
    )
    await bank_account.insert()
    return build_bank_account_response(bank_account)


@router.get("/bank-account", response_model=BankAccountResponse)
async def get_my_bank_account(current_artisan: User = Depends(get_current_artisan)):
    bank_account = await BankAccount.find_one({"user.$id": current_artisan.id})
    if not bank_account:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No bank account on file.")
    return build_bank_account_response(bank_account)
