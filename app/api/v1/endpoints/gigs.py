# app/api/v1/endpoints/gigs.py
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from beanie import PydanticObjectId

from app.api.deps import get_current_artisan
from app.models.gig import Gig, GigCreate, GigResponse, GigUpdate
from app.models.user import User

router = APIRouter()


def build_gig_response(gig: Gig) -> GigResponse:
    artisan_id = str(
        gig.artisan.ref.id if hasattr(gig.artisan, "ref") else gig.artisan.id
    )
    return GigResponse(
        id=str(gig.id),
        artisan_id=artisan_id,
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
    current_artisan: User = Depends(get_current_artisan),
):
    """Create a new service listing (Artisans only)."""
    gig = Gig(artisan=current_artisan, **gig_in.model_dump())
    await gig.insert()
    return build_gig_response(gig)


@router.get("/", response_model=List[GigResponse])
async def list_gigs(
    category: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 20,
    skip: int = 0,
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
async def get_my_gigs(current_artisan: User = Depends(get_current_artisan)):
    """Fetch all gigs created by the logged-in artisan."""
    gigs = await Gig.find({"artisan.$id": current_artisan.id}).to_list()
    return [build_gig_response(g) for g in gigs]


@router.get("/{gig_id}", response_model=GigResponse)
async def get_gig(gig_id: str):
    """Get single gig by ID."""
    gig = await Gig.get(PydanticObjectId(gig_id))
    if not gig:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gig not found"
        )
    return build_gig_response(gig)


@router.patch("/{gig_id}", response_model=GigResponse)
async def update_gig(
    gig_id: str,
    gig_in: GigUpdate,
    current_artisan: User = Depends(get_current_artisan),
):
    """Update a gig (Owner artisan only)."""
    gig = await Gig.get(PydanticObjectId(gig_id))
    if not gig:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gig not found"
        )

    artisan_id = (
        gig.artisan.ref.id if hasattr(gig.artisan, "ref") else gig.artisan.id
    )
    if artisan_id != current_artisan.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only edit your own gigs",
        )

    update_data = gig_in.model_dump(exclude_unset=True)
    if update_data:
        await gig.set(update_data)

    return build_gig_response(gig)


@router.delete("/{gig_id}", status_code=status.HTTP_200_OK)
async def delete_gig(
    gig_id: str,
    current_artisan: User = Depends(get_current_artisan),
):
    """Delete a gig (Owner artisan only)."""
    gig = await Gig.get(PydanticObjectId(gig_id))
    if not gig:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Gig not found"
        )

    artisan_id = (
        gig.artisan.ref.id if hasattr(gig.artisan, "ref") else gig.artisan.id
    )
    if artisan_id != current_artisan.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own gigs",
        )

    await gig.delete()
    return {"detail": "Gig deleted successfully"}