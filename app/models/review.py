# app/models/review.py
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from pydantic import BaseModel, Field

from app.models.booking import Booking
from app.models.user import User


class Review(Document):
    booking: Link[Booking]
    client: Link[User]
    artisan: Link[User]
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None
    client_name: str
    is_verified_booking: bool = True
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "reviews"
        indexes = ["booking", "artisan"]


class ReviewCreate(BaseModel):
    booking_id: str
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


class ReviewResponse(BaseModel):
    id: str
    booking_id: str
    artisan_id: str
    rating: int
    comment: Optional[str] = None
    client_name: str
    is_verified_booking: bool
    created_at: datetime
