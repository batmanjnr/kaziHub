# app/services/payouts.py
"""Artisan disbursement (spec §11.4): triggers a real Paystack Transfer to
the artisan's verified bank account on completion, rather than silently
recording a 'successful' local ledger entry with no money behind it.

Deliberately pure I/O — this never touches our DB. Callers record the
Transaction row themselves, inside the same booking-transition transaction,
using the status/reference returned here. That keeps the external HTTP call
outside the Mongo transaction (which can't roll back a real transfer anyway)
while still writing exactly one ledger entry per attempt.
"""
import secrets

from app.models.bank_account import BankAccount
from app.services.paystack import PaystackError, initiate_transfer


async def request_artisan_payout(artisan_user_id, amount: float, reason: str) -> dict:
    """Returns {"status": "pending"|"failed", "reference": str|None,
    "gateway_response": dict}. "pending" means Paystack accepted the
    transfer request; final settlement is confirmed later by the
    transfer.success/failed/reversed webhook (see wallet.py)."""
    bank_account = await BankAccount.find_one({"user.$id": artisan_user_id})
    if not bank_account or not bank_account.is_verified or not bank_account.paystack_recipient_code:
        return {
            "status": "failed",
            "reference": None,
            "gateway_response": {"error": "Artisan has no verified payout bank account on file."},
        }

    reference = f"KZ-PAYOUT-{secrets.token_hex(6).upper()}"
    try:
        response = await initiate_transfer(
            amount_kobo=int(round(amount * 100)),
            recipient_code=bank_account.paystack_recipient_code,
            reason=reason,
            reference=reference,
        )
        return {"status": "pending", "reference": reference, "gateway_response": response}
    except PaystackError as e:
        return {
            "status": "failed",
            "reference": reference,
            "gateway_response": {"error": str(e), **e.response_data},
        }
