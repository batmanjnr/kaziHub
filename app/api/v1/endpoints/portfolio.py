# app/api/v1/endpoints/portfolio.py
from typing import List
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.deps import get_own_artisan_profile
from app.core.cloudinary import delete_file_from_cloudinary, upload_file_to_cloudinary
from app.core.upload_validation import IMAGE_TYPES, validate_upload
from app.models.portfolio import (
    PortfolioItem,
    PortfolioItemCreate,
    PortfolioItemResponse,
    PortfolioItemUpdate,
)
from app.models.uploads import UploadUrlResponse
from app.models.profile import Profile
from app.services.moderation import moderate_image

router = APIRouter()


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


@router.post("/upload", response_model=UploadUrlResponse)
async def upload_portfolio_image(
    file: UploadFile = File(...),
    profile: Profile = Depends(get_own_artisan_profile),
):
    """Upload a portfolio image (Artisans only). Multipart, one `file`
    field: JPEG, PNG or WebP, up to 10 MB. Returns {"url": ...} to send as
    `image_url` when creating or editing a portfolio item."""
    await validate_upload(file, allowed_types=IMAGE_TYPES)
    secure_url = await upload_file_to_cloudinary(file, folder="kazihub/portfolio")
    if not await moderate_image(secure_url):
        await delete_file_from_cloudinary(secure_url)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This image did not pass content moderation.",
        )
    return {"url": secure_url}


@router.post("/", response_model=PortfolioItemResponse, status_code=status.HTTP_201_CREATED)
async def create_portfolio_item(
    item_in: PortfolioItemCreate,
    profile: Profile = Depends(get_own_artisan_profile),
):
    """Add a portfolio item (Artisans only)."""
    item = PortfolioItem(artisan_profile=profile, **item_in.model_dump())
    await item.insert()
    return build_portfolio_response(item)


@router.get("/", response_model=List[PortfolioItemResponse])
async def list_my_portfolio(profile: Profile = Depends(get_own_artisan_profile)):
    """List the logged-in artisan's own portfolio items."""
    items = await PortfolioItem.find({"artisan_profile.$id": profile.id}).to_list()
    return [build_portfolio_response(i) for i in items]


async def _load_own_item(item_id: str, profile: Profile) -> PortfolioItem:
    try:
        item = await PortfolioItem.get(PydanticObjectId(item_id))
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid portfolio item ID.")
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio item not found")
    artisan_profile_id = (
        item.artisan_profile.ref.id if hasattr(item.artisan_profile, "ref") else item.artisan_profile.id
    )
    if artisan_profile_id != profile.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage your own portfolio items",
        )
    return item


@router.patch("/{item_id}", response_model=PortfolioItemResponse)
async def update_portfolio_item(
    item_id: str,
    item_in: PortfolioItemUpdate,
    profile: Profile = Depends(get_own_artisan_profile),
):
    """Edit any subset of a portfolio item's fields, keeping its id and
    created_at (ask 7). Replacing image_url deletes the old Cloudinary
    image."""
    item = await _load_own_item(item_id, profile)
    update_data = item_in.model_dump(exclude_unset=True)
    old_image = item.image_url
    if update_data:
        await item.set(update_data)
    new_image = update_data.get("image_url")
    if new_image and new_image != old_image and old_image and "cloudinary.com" in old_image:
        try:
            await delete_file_from_cloudinary(old_image)
        except Exception:
            pass
    return build_portfolio_response(item)


@router.delete("/{item_id}", status_code=status.HTTP_200_OK)
async def delete_portfolio_item(
    item_id: str,
    profile: Profile = Depends(get_own_artisan_profile),
):
    """Delete a portfolio item (owner artisan only)."""
    item = await PortfolioItem.get(PydanticObjectId(item_id))
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio item not found")

    artisan_profile_id = (
        item.artisan_profile.ref.id
        if hasattr(item.artisan_profile, "ref")
        else item.artisan_profile.id
    )
    if artisan_profile_id != profile.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own portfolio items",
        )

    if item.image_url and "cloudinary.com" in item.image_url:
        await delete_file_from_cloudinary(item.image_url)

    await item.delete()
    return {"detail": "Portfolio item deleted successfully"}
