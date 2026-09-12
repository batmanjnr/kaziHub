# app/api/v1/endpoints/reviews.py
from typing import List
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_user
from app.models.booking import Booking, BookingStatus
from app.models.notification import Notification
from app.models.profile import Profile
from app.models.review import Review, ReviewCreate, ReviewResponse
from app.models.user import User

router = APIRouter()


def build_review_response(review: Review) -> ReviewResponse:
    return ReviewResponse(
        id=str(review.id),
        booking_id=str(
            review.booking.ref.id if hasattr(review.booking, "ref") else review.booking.id
        ),
        artisan_id=str(
            review.artisan.ref.id if hasattr(review.artisan, "ref") else review.artisan.id
        ),
        rating=review.rating,
        comment=review.comment,
        client_name=review.client_name,
        is_verified_booking=review.is_verified_booking,
        created_at=review.created_at,
    )


@router.post("/", response_model=ReviewResponse, status_code=status.HTTP_201_CREATED)
async def create_review(
    payload: ReviewCreate,
    current_user: User = Depends(get_current_user),
):
    """Client rates a completed booking (spec §5.6)."""
    try:
        booking = await Booking.get(PydanticObjectId(payload.booking_id))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid booking ID.")
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Booking not found.")

    client_id = str(
        booking.client.ref.id if hasattr(booking.client, "ref") else booking.client.id
    )
    if client_id != str(current_user.id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Only the client on this booking can leave a review.",
        )

    if booking.status != BookingStatus.PAID_OUT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="You can only review a completed booking."
        )

    existing = await Review.find_one({"booking.$id": booking.id})
    if existing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="This booking has already been reviewed."
        )

    artisan_id = booking.artisan.ref.id if hasattr(booking.artisan, "ref") else booking.artisan.id
    artisan = await User.get(artisan_id)

    review = Review(
        booking=booking,
        client=current_user,
        artisan=artisan,
        rating=payload.rating,
        comment=payload.comment,
        client_name=f"{current_user.first_name} {current_user.last_name}",
    )
    await review.insert()

    # Recompute the artisan's aggregate rating/review count.
    profile = await Profile.find_one({"user.$id": artisan_id})
    if profile:
        new_count = profile.review_count + 1
        new_avg = ((profile.rating_average * profile.review_count) + payload.rating) / new_count
        profile.rating_average = round(new_avg, 2)
        profile.review_count = new_count
        await profile.save()

    if artisan:
        await Notification(
            user=artisan,
            type="new_review",
            title="New review received",
            message=f"{review.client_name} left you a {payload.rating}-star review.",
            booking=booking,
        ).insert()

    return build_review_response(review)


@router.get("/pro/{pro_id}", response_model=List[ReviewResponse])
async def get_reviews_for_professional(
    pro_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Public list of reviews for an artisan."""
    try:
        artisan_object_id = PydanticObjectId(pro_id)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid professional ID.")

    reviews = (
        await Review.find({"artisan.$id": artisan_object_id})
        .sort("-created_at")
        .skip(offset)
        .limit(limit)
        .to_list()
    )
    return [build_review_response(r) for r in reviews]
