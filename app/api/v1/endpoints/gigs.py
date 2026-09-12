# app/api/v1/endpoints/gigs.py
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File
from beanie import PydanticObjectId

from app.api.deps import get_current_artisan, get_own_artisan_profile
from app.models.gig import Gig, GigCreate, GigResponse, GigUpdate
from app.models.profile import Profile
from app.models.user import User
from app.core.cloudinary import delete_file_from_cloudinary, upload_file_to_cloudinary
from app.core.upload_validation import IMAGE_TYPES, validate_upload
from app.services.moderation import moderate_image

router = APIRouter()


@router.post("/upload")
async def upload_gig_image(
    file: UploadFile = File(...),
    current_artisan: User = Depends(get_current_artisan),
):
    """Upload gig images to Cloudinary (Artisans only)."""
    await validate_upload(file, allowed_types=IMAGE_TYPES)
    secure_url = await upload_file_to_cloudinary(file, folder="kazihub/gigs")
    if not await moderate_image(secure_url):
        await delete_file_from_cloudinary(secure_url)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This image did not pass content moderation.",
        )
    return {"url": secure_url}


def build_gig_response(gig: Gig) -> GigResponse:
    artisan_profile_id = str(
        gig.artisan_profile.ref.id
        if hasattr(gig.artisan_profile, "ref")
        else gig.artisan_profile.id
    )
    return GigResponse(
        id=str(gig.id),
        artisan_profile_id=artisan_profile_id,
        title=gig.title,
        description=gig.description,
        category=gig.category,
        tags=gig.tags,
        price=gig.price,
        delivery_time_days=gig.delivery_time_days,
        images=gig.images,
        is_active=gig.is_active,
        created_at=gig.created_at,
    )


@router.post("/", response_model=GigResponse, status_code=status.HTTP_201_CREATED)
async def create_gig(
    gig_in: GigCreate,
    profile: Profile = Depends(get_own_artisan_profile),
):
    """Create a new service listing (Artisans only)."""
    gig = Gig(artisan_profile=profile, **gig_in.model_dump())
    await gig.insert()
    return build_gig_response(gig)


@router.get("/", response_model=List[GigResponse])
async def list_gigs(
    category: Optional[str] = None,
    tag: Optional[str] = None,
    # FIX (security review): unbounded before — a client could pass an
    # arbitrarily large `limit` and force the server to fetch/serialize
    # the entire public gig catalog in one response.
    limit: int = Query(default=20, ge=1, le=100),
    skip: int = Query(default=0, ge=0),
):
    """Public endpoint to browse active service listings."""
    query = {"is_active": True}
    if category:
        query["category"] = category
    if tag:
        query["tags"] = tag

    gigs = await Gig.find(query).skip(skip).limit(limit).to_list()
    return [build_gig_response(g) for g in gigs]


@router.get("/my-gigs", response_model=List[GigResponse])
async def get_my_gigs(profile: Profile = Depends(get_own_artisan_profile)):
    """Fetch all gigs created by the logged-in artisan."""
    gigs = await Gig.find({"artisan_profile.$id": profile.id}).to_list()
    return [build_gig_response(g) for g in gigs]


@router.get("/{gig_id}", response_model=GigResponse)
async def get_gig(gig_id: str):
    """Get single gig by ID. Increments the public view counter."""
    gig = await Gig.get(PydanticObjectId(gig_id))
    if not gig:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gig not found"
        )
    gig.views_count += 1
    await gig.save()
    return build_gig_response(gig)


def _assert_owns_gig(gig: Gig, profile: Profile) -> None:
    artisan_profile_id = (
        gig.artisan_profile.ref.id
        if hasattr(gig.artisan_profile, "ref")
        else gig.artisan_profile.id
    )
    if artisan_profile_id != profile.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage your own gigs",
        )


@router.patch("/{gig_id}", response_model=GigResponse)
async def update_gig(
    gig_id: str,
    gig_in: GigUpdate,
    profile: Profile = Depends(get_own_artisan_profile),
):
    """Update a gig (Owner artisan only)."""
    gig = await Gig.get(PydanticObjectId(gig_id))
    if not gig:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gig not found"
        )
    _assert_owns_gig(gig, profile)

    update_data = gig_in.model_dump(exclude_unset=True)
    if update_data:
        await gig.set(update_data)

    return build_gig_response(gig)


@router.delete("/{gig_id}", status_code=status.HTTP_200_OK)
async def delete_gig(
    gig_id: str,
    profile: Profile = Depends(get_own_artisan_profile),
):
    """Delete a gig (Owner artisan only)."""
    gig = await Gig.get(PydanticObjectId(gig_id))
    if not gig:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gig not found"
        )
    _assert_owns_gig(gig, profile)

    await gig.delete()
    return {"detail": "Gig deleted successfully"}
