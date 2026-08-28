from datetime import datetime
from enum import Enum
from typing import Optional
from beanie import Document, Link
from app.models.booking import Booking
from app.models.user import User


class TransactionType(str, Enum):
    ESCROW_DEPOSIT = "escrow_deposit"
    ESCROW_RELEASE = "escrow_release"
    REFUND = "refund"


class TransactionStatus(str, Enum):
    PENDING = "pending"
    SUCCESSFUL = "successful"
    FAILED = "failed"


class Transaction(Document):
    user: Link[User]
    booking: Link[Booking]
    reference: str
    amount: float
    type: TransactionType
    status: TransactionStatus = TransactionStatus.PENDING
    gateway_response: Optional[str] = None
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "transactions"