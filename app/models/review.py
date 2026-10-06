# app/models/review.py
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from app.core.time import utc_now
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
    # Landing-page testimonials (ask 40): shown publicly only when the
    # client consented (share_publicly), featured ones first.
    share_publicly: bool = False
    photo_url: Optional[str] = None  # only ever shown when share_publicly
    is_featured: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "reviews"
        indexes = ["booking", "artisan"]


class ReviewCreate(BaseModel):
    booking_id: str
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = Field(default=None, max_length=1000)
    share_publicly: bool = Field(
        default=False,
        description="Client agrees to this review appearing on the public landing page "
        "with their first name and state.",
    )
    photo_url: Optional[str] = Field(
        default=None, description="Optional photo to show with the public review (from POST /bookings/upload)."
    )


class ReviewSharingUpdate(BaseModel):
    share_publicly: bool


class FeaturedReviewResponse(BaseModel):
    id: str
    rating: int
    comment: Optional[str] = None
    client_first_name: str
    client_area: Optional[str] = None
    category: Optional[str] = None
    photo_url: Optional[str] = None
    is_featured: bool
    created_at: datetime


class ReviewResponse(BaseModel):
    id: str
    booking_id: str
    artisan_id: str
    rating: int
    comment: Optional[str] = None
    client_name: str
    is_verified_booking: bool
    share_publicly: bool = False
    created_at: datetime
