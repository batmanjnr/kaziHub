# app/models/bank_account.py
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field

from app.models.user import User


class BankAccount(Document):
    user: Link[User]
    bank_code: str
    bank_name: str
    account_number: str
    account_name: str
    paystack_recipient_code: Optional[str] = None
    is_verified: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "artisan_bank_accounts"
        indexes = ["user"]


class BankAccountCreate(BaseModel):
    bank_code: str
    account_number: str


class BankAccountResponse(BaseModel):
    id: str
    bank_code: str
    bank_name: str
    account_number: str
    account_name: str
    is_verified: bool
    created_at: datetime
