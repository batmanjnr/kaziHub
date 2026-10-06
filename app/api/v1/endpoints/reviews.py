# app/api/v1/endpoints/reviews.py
from typing import List
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_user
from app.models.booking import Booking, BookingStatus
from app.models.profile import Profile
from app.models.review import (
    FeaturedReviewResponse,
    Review,
    ReviewCreate,
    ReviewResponse,
    ReviewSharingUpdate,
)
from app.models.user import User
from app.services.notifications import notify

router = APIRouter()


async def resolve_artisan_user_id(pro_id: str) -> PydanticObjectId:
    """Accepts an artisan's user id or their profile id (ask 22)."""
    try:
        oid = PydanticObjectId(pro_id)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid professional ID.")
    if await User.get(oid):
        return oid
    profile = await Profile.get(oid)
    if profile:
        return profile.user.ref.id if hasattr(profile.user, "ref") else profile.user.id
    return oid


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
        share_publicly=review.share_publicly,
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
        share_publicly=payload.share_publicly,
        photo_url=payload.photo_url,
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

    await notify(
        artisan, "new_review", "New review received",
        f"{current_user.first_name} left you a {payload.rating}-star review.", booking=booking,
    )

    return build_review_response(review)


@router.get("/featured", response_model=List[FeaturedReviewResponse])
async def featured_reviews(limit: int = Query(default=6, ge=1, le=20)):
    """Public testimonials for the landing page (ask 40). Only reviews whose
    client agreed to share them publicly (`share_publicly`), all from
    completed (paid_out) bookings. Admin-featured reviews come first, then
    other 4-5 star ones, newest first. Shows the client's first name and
    state only (state hidden if they turned off share_neighborhood)."""
    featured = (
        await Review.find({"share_publicly": True, "is_featured": True})
        .sort("-created_at").limit(limit).to_list()
    )
    if len(featured) < limit:
        featured += (
            await Review.find({"share_publicly": True, "is_featured": {"$ne": True}, "rating": {"$gte": 4}})
            .sort("-created_at").limit(limit - len(featured)).to_list()
        )

    out = []
    for r in featured:
        client = await User.get(r.client.ref.id if hasattr(r.client, "ref") else r.client.id)
        if not client or client.deleted_at is not None:
            continue
        artisan_id = r.artisan.ref.id if hasattr(r.artisan, "ref") else r.artisan.id
        profile = await Profile.find_one({"user.$id": artisan_id})
        out.append(
            FeaturedReviewResponse(
                id=str(r.id),
                rating=r.rating,
                comment=r.comment,
                client_first_name=client.first_name,
                client_area=client.state if client.share_neighborhood else None,
                category=(profile.category or None) if profile else None,
                photo_url=r.photo_url,
                is_featured=r.is_featured,
                created_at=r.created_at,
            )
        )
    return out


@router.patch("/{review_id}/sharing", response_model=ReviewResponse)
async def update_review_sharing(
    review_id: str, payload: ReviewSharingUpdate, current_user: User = Depends(get_current_user)
):
    """The review's author turns public sharing on or off at any time."""
    try:
        review = await Review.get(PydanticObjectId(review_id))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid review ID.")
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Review not found.")
    author_id = review.client.ref.id if hasattr(review.client, "ref") else review.client.id
    if author_id != current_user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only the review's author can change this.")
    review.share_publicly = payload.share_publicly
    await review.save()
    return build_review_response(review)


@router.get("/pro/{pro_id}", response_model=List[ReviewResponse])
async def get_reviews_for_professional(
    pro_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Public list of reviews for an artisan. `pro_id` is the artisan's user
    id; their profile id also works."""
    artisan_object_id = await resolve_artisan_user_id(pro_id)

    reviews = (
        await Review.find({"artisan.$id": artisan_object_id})
        .sort("-created_at")
        .skip(offset)
        .limit(limit)
        .to_list()
    )
    return [build_review_response(r) for r in reviews]
