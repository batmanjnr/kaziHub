from datetime import datetime
from enum import Enum
from typing import Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import Field
from app.models.booking import Booking


class TransactionType(str, Enum):
    ESCROW_DEPOSIT = "escrow_deposit"
    ESCROW_RELEASE = "escrow_release"
    REFUND = "refund"
    PLATFORM_FEE = "platform_fee"
    GATEWAY_FEE = "gateway_fee"


class TransactionStatus(str, Enum):
    PENDING = "pending"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    REVERSED = "reversed"


class Transaction(Document):
    booking: Link[Booking]
    # FIX: renamed from `reference` to match spec's `transaction_reference` —
    # this is the escrow_transactions ledger, keyed only to a booking (no
    # user_id column in the spec table).
    transaction_reference: str
    gateway: str = "paystack"
    amount: float
    currency: str = "NGN"
    type: TransactionType
    status: TransactionStatus = TransactionStatus.PENDING
    gateway_response: Optional[dict] = None
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "escrow_transactions"
        indexes = ["transaction_reference", "booking"]
