# app/api/v1/endpoints/profiles.py
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_artisan, get_current_user
from app.models.profile import Profile, ProfileCreate, ProfileResponse, ProfileUpdate
from app.models.user import User

router = APIRouter()


def build_profile_response(profile: Profile) -> ProfileResponse:
    user_id = str(
        profile.user.ref.id if hasattr(profile.user, "ref") else profile.user.id
    )

    return ProfileResponse(
        id=str(profile.id),
        user_id=user_id,
        category=profile.category,
        skills=profile.skills,
        bio=profile.bio,
        hourly_rate=profile.hourly_rate,
        years_of_experience=profile.years_of_experience,
        address=profile.address,
        city=profile.city,
        state=profile.state,
        is_available=profile.is_available,
        is_verified=profile.is_verified,
    )


@router.post("/", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_or_update_profile(
    profile_in: ProfileCreate,
    current_user: User = Depends(get_current_artisan),  # Requires artisan role
):
    """Create or update profile (Artisans only)."""
    existing_profile = await Profile.find_one({"user.$id": current_user.id})

    if existing_profile:
        await existing_profile.set(profile_in.model_dump(exclude_unset=True))
        return build_profile_response(existing_profile)

    profile = Profile(
        user=current_user, state=current_user.state, **profile_in.model_dump()
    )
    await profile.insert()
    return build_profile_response(profile)


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
    return build_profile_response(profile)


@router.patch("/me", response_model=ProfileResponse)
async def update_my_profile(
    profile_in: ProfileUpdate,
    current_user: User = Depends(get_current_artisan),  # Requires artisan role
):
    """Update profile fields (Artisans only)."""
    profile = await Profile.find_one({"user.$id": current_user.id})
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found"
        )

    update_data = profile_in.model_dump(exclude_unset=True)
    if update_data:
        await profile.set(update_data)

    return build_profile_response(profile)


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


@router.get("/all", response_model=List[ProfileResponse])
async def list_profiles(
    category: Optional[str] = None, limit: int = 20
):
    """Public search endpoint to browse available artisans (accessible to everyone)."""
    query = {}
    if category:
        query["category"] = category

    profiles = await Profile.find(query).limit(limit).to_list()
    return [build_profile_response(p) for p in profiles]