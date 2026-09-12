# app/api/v1/endpoints/services.py
from typing import List
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_own_artisan_profile
from app.models.profile import Profile, PRICING_TYPES
from app.models.service import Service, ServiceCreate, ServiceResponse, ServiceUpdate

router = APIRouter()


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


def _validate_pricing_type(pricing_type: str) -> None:
    if pricing_type not in PRICING_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"pricing_type must be one of {PRICING_TYPES}",
        )


@router.post("/", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_service(
    service_in: ServiceCreate,
    profile: Profile = Depends(get_own_artisan_profile),
):
    """Add a service to the artisan's catalog (Artisans only)."""
    _validate_pricing_type(service_in.pricing_type)
    service = Service(artisan_profile=profile, **service_in.model_dump())
    await service.insert()
    return build_service_response(service)


@router.get("/", response_model=List[ServiceResponse])
async def list_my_services(profile: Profile = Depends(get_own_artisan_profile)):
    """List the logged-in artisan's own services, including inactive ones."""
    services = await Service.find({"artisan_profile.$id": profile.id}).to_list()
    return [build_service_response(s) for s in services]


def _assert_owns_service(service: Service, profile: Profile) -> None:
    artisan_profile_id = (
        service.artisan_profile.ref.id
        if hasattr(service.artisan_profile, "ref")
        else service.artisan_profile.id
    )
    if artisan_profile_id != profile.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage your own services",
        )


@router.patch("/{service_id}", response_model=ServiceResponse)
async def update_service(
    service_id: str,
    service_in: ServiceUpdate,
    profile: Profile = Depends(get_own_artisan_profile),
):
    service = await Service.get(PydanticObjectId(service_id))
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    _assert_owns_service(service, profile)

    update_data = service_in.model_dump(exclude_unset=True)
    if "pricing_type" in update_data and update_data["pricing_type"]:
        _validate_pricing_type(update_data["pricing_type"])
    if update_data:
        await service.set(update_data)

    return build_service_response(service)


@router.delete("/{service_id}", status_code=status.HTTP_200_OK)
async def delete_service(
    service_id: str,
    profile: Profile = Depends(get_own_artisan_profile),
):
    service = await Service.get(PydanticObjectId(service_id))
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    _assert_owns_service(service, profile)

    await service.delete()
    return {"detail": "Service deleted successfully"}
