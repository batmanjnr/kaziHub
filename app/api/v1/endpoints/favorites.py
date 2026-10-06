# app/api/v1/endpoints/favorites.py
from typing import List
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.api.v1.endpoints.reviews import resolve_artisan_user_id
from app.models.favorite import FavoriteResponse, SavedProfessional
from app.models.profile import Profile
from app.models.user import User

router = APIRouter()

PRO_ID_DOC = (
    "`pro_id` is the artisan's **user id** (a profile's `user_id`); their profile id is also accepted."
)


@router.get("/", response_model=List[FavoriteResponse])
async def list_favorites(current_user: User = Depends(get_current_user)):
    """Saved artisans. `artisan_id` is the artisan's user id."""
    saved = (
        await SavedProfessional.find({"user.$id": current_user.id})
        .sort("-created_at")
        .to_list()
    )
    results = []
    for s in saved:
        artisan_id = s.artisan.ref.id if hasattr(s.artisan, "ref") else s.artisan.id
        profile = await Profile.find_one({"user.$id": artisan_id})
        artisan_user = await User.get(artisan_id)
        results.append(
            FavoriteResponse(
                artisan_id=str(artisan_id),
                business_name=profile.business_name if profile else None,
                category=profile.category if profile else None,
                rating_average=profile.rating_average if profile else None,
                avatar_url=artisan_user.profile_picture if artisan_user else None,
                created_at=s.created_at,
            )
        )
    return results


@router.post("/{pro_id}", status_code=status.HTTP_201_CREATED, description=PRO_ID_DOC)
async def save_professional(pro_id: str, current_user: User = Depends(get_current_user)):
    artisan_id = await resolve_artisan_user_id(pro_id)

    artisan = await User.get(artisan_id)
    if not artisan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Professional not found.")

    if str(current_user.id) == str(artisan_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="You cannot save yourself.")

    existing = await SavedProfessional.find_one(
        {"user.$id": current_user.id, "artisan.$id": artisan_id}
    )
    if existing:
        return {"detail": "Already saved."}

    await SavedProfessional(user=current_user, artisan=artisan).insert()
    return {"detail": "Professional saved."}


@router.delete("/{pro_id}", status_code=status.HTTP_200_OK, description=PRO_ID_DOC)
async def unsave_professional(pro_id: str, current_user: User = Depends(get_current_user)):
    artisan_id = await resolve_artisan_user_id(pro_id)

    existing = await SavedProfessional.find_one(
        {"user.$id": current_user.id, "artisan.$id": artisan_id}
    )
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not in favorites.")
    await existing.delete()
    return {"detail": "Removed from favorites."}
