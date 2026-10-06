# app/api/v1/endpoints/profiles.py
import asyncio
from typing import Annotated, Dict, Iterable, List, Optional
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.api.deps import get_current_artisan, get_current_user
from app.models.portfolio import PortfolioItem, PortfolioItemResponse
from app.models.profile import (
    Profile,
    ProfileCreate,
    ProfileResponse,
    ProfileUpdate,
    make_geo_point,
)
from app.models.review import Review, ReviewResponse
from app.models.service import Service, ServiceResponse
from app.models.user import User
from app.core.time import utc_now

router = APIRouter()

MAX_LIMIT = 100
DEFAULT_LIMIT = 20


def profile_user_id(profile: Profile) -> PydanticObjectId:
    return profile.user.ref.id if hasattr(profile.user, "ref") else profile.user.id


async def load_owners(profiles: Iterable[Profile]) -> Dict[PydanticObjectId, User]:
    """One query for the owning users of a page of profiles (ask 1)."""
    ids = list({profile_user_id(p) for p in profiles})
    if not ids:
        return {}
    users = await User.find({"_id": {"$in": ids}}).to_list()
    return {u.id: u for u in users}


def build_profile_response(
    profile: Profile, for_owner: bool = True, owner: Optional[User] = None
) -> ProfileResponse:
    user_id = str(profile_user_id(profile))

    # The owner always sees their own neighborhood; public callers (search
    # results, profile detail) only see it when share_neighborhood is on.
    show_neighborhood = for_owner or profile.share_neighborhood

    return ProfileResponse(
        id=str(profile.id),
        user_id=user_id,
        first_name=owner.first_name if owner else None,
        last_name=owner.last_name if owner else None,
        profile_picture=owner.profile_picture if owner else None,
        business_name=profile.business_name,
        category=profile.category,
        tagline=profile.tagline,
        skills=profile.skills,
        bio=profile.bio,
        hourly_rate=profile.hourly_rate,
        base_price=profile.base_price,
        pricing_type=profile.pricing_type,
        years_of_experience=profile.years_of_experience,
        address=profile.address,
        city=profile.city,
        neighborhood=profile.neighborhood if show_neighborhood else None,
        state=profile.state,
        latitude=profile.latitude,
        longitude=profile.longitude,
        is_available=profile.is_available,
        is_available_now=profile.is_available_now,
        availability_status=profile.availability_status,
        is_verified=profile.is_verified,
        rating_average=profile.rating_average,
        review_count=profile.review_count,
        completed_jobs_count=profile.completed_jobs_count,
        response_time=profile.response_time,
        insurance_backed=profile.insurance_backed,
        phone_visibility=profile.phone_visibility,
        share_neighborhood=profile.share_neighborhood,
        is_paused=profile.is_paused,
    )


class ProfileListMeta(BaseModel):
    total: int
    limit: int
    offset: int
    has_more: bool


class ProfileListResponse(BaseModel):
    data: List[ProfileResponse]
    meta: ProfileListMeta


class PublicProfileDetailResponse(ProfileResponse):
    services: List[ServiceResponse]
    portfolio: List[PortfolioItemResponse]
    reviews: List[ReviewResponse]


def build_service_response(service: Service) -> ServiceResponse:
    artisan_profile_id = str(
        service.artisan_profile.ref.id
        if hasattr(service.artisan_profile, "ref")
        else service.artisan_profile.id
    )
    return ServiceResponse(
        id=str(service.id),
        artisan_profile_id=artisan_profile_id,
        name=service.name,
        category=service.category,
        description=service.description,
        pricing_type=service.pricing_type,
        price=service.price,
        duration_estimate=service.duration_estimate,
        is_active=service.is_active,
        created_at=service.created_at,
    )


def build_portfolio_response(item: PortfolioItem) -> PortfolioItemResponse:
    artisan_profile_id = str(
        item.artisan_profile.ref.id
        if hasattr(item.artisan_profile, "ref")
        else item.artisan_profile.id
    )
    return PortfolioItemResponse(
        id=str(item.id),
        artisan_profile_id=artisan_profile_id,
        title=item.title,
        category=item.category,
        image_url=item.image_url,
        description=item.description,
        date_completed=item.date_completed,
        created_at=item.created_at,
    )


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


@router.get("/", response_model=ProfileListResponse)
async def list_profiles(
    category: Optional[str] = None,
    neighborhood: Optional[str] = None,
    state: Optional[str] = None,
    min_rating: Optional[float] = Query(default=None, ge=0, le=5),
    min_experience: Optional[int] = Query(default=None, ge=0),
    available_only: Annotated[
        bool, Query(description="Only artisans accepting new work (is_available), online or not.")
    ] = False,
    online_now: Annotated[
        bool, Query(description="Only artisans online right now (availability_status 'Available').")
    ] = False,
    verified_only: Annotated[bool, Query(description="Only ID-verified artisans.")] = False,
    search: Optional[str] = None,
    near_lat: Optional[float] = None,
    near_lng: Optional[float] = None,
    radius_km: float = Query(default=25, gt=0, le=200),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
):
    """Public search for artisans.

    Lists verified and unverified artisans alike; show a badge when
    `is_verified` is true, or pass `verified_only=true` (ask 5). Frozen
    accounts and unfinished profiles (no category yet) are never listed,
    and `meta.total`/`has_more` count only what a customer can see (ask 2).
    """
    query: dict = {
        "is_paused": {"$ne": True},
        "category": {"$nin": ["", None]},
    }
    if category:
        query["category"] = category
    if neighborhood:
        query["neighborhood"] = neighborhood
    if state:
        query["state"] = state
    if min_rating is not None:
        query["rating_average"] = {"$gte": min_rating}
    if min_experience is not None:
        query["years_of_experience"] = {"$gte": min_experience}
    if available_only:
        query["is_available"] = True
    if online_now:
        query["availability_status"] = "Available"
    if verified_only:
        query["is_verified"] = True
    if search:
        # Portable case-insensitive substring match rather than the $text
        # index (which mongomock doesn't support and which can't combine
        # cleanly with a $geoNear stage below). Meilisearch is the spec's
        # own documented upgrade path once catalog size outgrows this.
        query["$or"] = [
            {"business_name": {"$regex": search, "$options": "i"}},
            {"bio": {"$regex": search, "$options": "i"}},
            {"tagline": {"$regex": search, "$options": "i"}},
        ]

    if near_lat is not None and near_lng is not None:
        collection = Profile.get_pymongo_collection()
        pipeline = [
            {
                "$geoNear": {
                    "near": {"type": "Point", "coordinates": [near_lng, near_lat]},
                    "distanceField": "_distance_meters",
                    "maxDistance": radius_km * 1000,
                    "spherical": True,
                    "query": query,
                }
            },
            {
                "$facet": {
                    "data": [{"$skip": offset}, {"$limit": limit}],
                    "total": [{"$count": "count"}],
                }
            },
        ]
        result = await collection.aggregate(pipeline).to_list(length=1)
        raw_docs = result[0]["data"] if result else []
        total = result[0]["total"][0]["count"] if result and result[0]["total"] else 0
        profiles = [Profile.model_validate(doc) for doc in raw_docs]
    else:
        # count() and find() are independent round trips — run them
        # concurrently instead of back-to-back (same reasoning as the
        # gather above: each round trip to a remote Atlas cluster is
        # costly, so halving how many happen serially matters).
        total, profiles = await asyncio.gather(
            Profile.find(query).count(),
            Profile.find(query)
            .sort("-rating_average")
            .skip(offset)
            .limit(limit)
            .to_list(),
        )

    owners = await load_owners(profiles)
    return ProfileListResponse(
        data=[
            build_profile_response(p, for_owner=False, owner=owners.get(profile_user_id(p)))
            for p in profiles
        ],
        meta=ProfileListMeta(
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(profiles) < total,
        ),
    )


@router.post("/", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_or_update_profile(
    profile_in: ProfileCreate,
    current_user: User = Depends(get_current_artisan),  # Requires artisan role
):
    """Create or update profile (Artisans only)."""
    _check_neighborhood(profile_in.neighborhood, current_user.state)
    existing_profile = await Profile.find_one({"user.$id": current_user.id})

    if existing_profile:
        await existing_profile.set(profile_in.model_dump(exclude_unset=True))
        return build_profile_response(existing_profile, owner=current_user)

    data = profile_in.model_dump()
    data = {k: v for k, v in data.items() if v is not None}
    profile = Profile(user=current_user, state=current_user.state, **data)
    await profile.insert()
    return build_profile_response(profile, owner=current_user)


def _check_neighborhood(neighborhood: Optional[str], state: str) -> None:
    if neighborhood and neighborhood.strip().lower() == (state or "").strip().lower():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="neighborhood should be the area within your state, not the state itself.",
        )


@router.get("/me", response_model=ProfileResponse)
async def get_my_profile(
    current_user: User = Depends(get_current_artisan),  # Requires artisan role
):
    """Fetch profile details for the logged-in artisan."""
    profile = await Profile.find_one({"user.$id": current_user.id})
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found"
        )
    return build_profile_response(profile, owner=current_user)


@router.put("/me", response_model=ProfileResponse)
async def update_my_profile(
    profile_in: ProfileUpdate,
    current_user: User = Depends(get_current_artisan),  # Requires artisan role
):
    """Update bio, rates, neighborhood, and availability (Artisans only)."""
    profile = await Profile.find_one({"user.$id": current_user.id})
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found"
        )

    update_data = profile_in.model_dump(exclude_unset=True)

    if update_data.get("neighborhood"):
        _check_neighborhood(update_data["neighborhood"], profile.state)

    # base_price > 0 unless quote_required, checked against whichever of
    # the two this request doesn't change (ask 37).
    pricing_type = update_data.get("pricing_type", profile.pricing_type)
    base_price = update_data.get("base_price", profile.base_price)
    if ("pricing_type" in update_data or "base_price" in update_data) and pricing_type != "quote_required":
        if base_price is None or base_price <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="base_price must be greater than 0 unless pricing_type is 'quote_required'.",
            )

    if "latitude" in update_data or "longitude" in update_data:
        lat = update_data.get("latitude", profile.latitude)
        lng = update_data.get("longitude", profile.longitude)
        update_data["geo_location"] = make_geo_point(lat, lng)

    if "availability_status" in update_data:
        update_data["is_available_now"] = update_data["availability_status"] == "Available"

    if update_data:
        update_data["updated_at"] = utc_now()
        await profile.set(update_data)

    return build_profile_response(profile, owner=current_user)


@router.delete("/me", status_code=status.HTTP_200_OK)
async def delete_my_profile(
    current_user: User = Depends(get_current_artisan),  # Requires artisan role
):
    """Delete profile (Artisans only)."""
    profile = await Profile.find_one({"user.$id": current_user.id})
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found"
        )

    await profile.delete()
    return {"detail": "Profile deleted successfully"}


@router.get("/{profile_id}", response_model=PublicProfileDetailResponse)
async def get_profile_detail(profile_id: str):
    """Full public profile: portfolio, catalog services, and reviews.

    Phone numbers are never included here; a client sees the artisan's
    number on their booking once phone_visibility allows it. A frozen
    artisan's page is 404, the same as their absence from search (ask 17).
    """
    try:
        profile = await Profile.get(PydanticObjectId(profile_id))
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid profile ID.")

    if not profile or profile.is_paused:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    artisan_user_id = profile_user_id(profile)
    # Independent of each other — issued concurrently instead of one round
    # trip after another, which matters a lot against a remote Atlas
    # cluster where each round trip alone can cost 100ms+.
    owner, services, portfolio, reviews = await asyncio.gather(
        User.get(artisan_user_id),
        Service.find({"artisan_profile.$id": profile.id, "is_active": True}).to_list(),
        PortfolioItem.find({"artisan_profile.$id": profile.id}).to_list(),
        Review.find({"artisan.$id": artisan_user_id})
        .sort("-created_at")
        .limit(20)
        .to_list(),
    )

    if owner and owner.is_paused:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    base = build_profile_response(profile, for_owner=False, owner=owner)
    return PublicProfileDetailResponse(
        **base.model_dump(),
        services=[build_service_response(s) for s in services],
        portfolio=[build_portfolio_response(p) for p in portfolio],
        reviews=[build_review_response(r) for r in reviews],
    )
