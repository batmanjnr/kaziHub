import pytest

from app.api.v1.endpoints.gigs import create_gig, get_gig
from app.api.v1.endpoints.portfolio import (
    create_portfolio_item,
    delete_portfolio_item,
    list_my_portfolio,
)
from app.api.v1.endpoints.profiles import get_profile_detail, list_profiles
from app.api.v1.endpoints.services import (
    create_service,
    delete_service,
    list_my_services,
    update_service,
)
from app.core.security import get_password_hash
from app.models.gig import GigCreate
from app.models.portfolio import PortfolioItemCreate
from app.models.profile import Profile
from app.models.service import ServiceCreate, ServiceUpdate
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def make_artisan_with_profile(email="artisan@example.com") -> Profile:
    user = User(
        first_name="Ade",
        last_name="Bello",
        email=email,
        phone_number="+2348000000001",
        state="Lagos",
        role="artisan",
        hashed_password=get_password_hash("Passw0rd!"),
    )
    await user.insert()
    profile = Profile(
        user=user,
        category="plumbing",
        state="Lagos",
        business_name="Ade Plumbing Co",
        rating_average=4.5,
    )
    await profile.insert()
    return profile


async def test_list_profiles_filters_by_category_and_paginates():
    p1 = await make_artisan_with_profile("a1@example.com")
    p1.category = "plumbing"
    await p1.save()

    p2 = await make_artisan_with_profile("a2@example.com")
    p2.category = "electrical"
    await p2.save()

    result = await list_profiles(
        category="plumbing",
        neighborhood=None,
        state=None,
        min_rating=None,
        min_experience=None,
        available_only=False,
        search=None,
        near_lat=None,
        near_lng=None,
        radius_km=25,
        limit=20,
        offset=0,
    )
    assert result.meta.total == 1
    assert result.data[0].category == "plumbing"


async def test_service_crud_is_scoped_to_own_profile():
    profile = await make_artisan_with_profile()

    created = await create_service(
        ServiceCreate(name="Pipe Fix", category="plumbing", pricing_type="fixed", price=5000),
        profile=profile,
    )
    assert created.name == "Pipe Fix"

    services = await list_my_services(profile=profile)
    assert len(services) == 1

    from app.models.service import Service

    service_doc = await Service.get(created.id)
    updated = await update_service(
        str(service_doc.id), ServiceUpdate(price=7500), profile=profile
    )
    assert updated.price == 7500

    await delete_service(str(service_doc.id), profile=profile)
    remaining = await list_my_services(profile=profile)
    assert remaining == []


async def test_portfolio_crud():
    profile = await make_artisan_with_profile()

    created = await create_portfolio_item(
        PortfolioItemCreate(
            title="Kitchen repipe",
            category="plumbing",
            image_url="https://example.com/photo.jpg",
        ),
        profile=profile,
    )
    items = await list_my_portfolio(profile=profile)
    assert len(items) == 1

    await delete_portfolio_item(created.id, profile=profile)
    remaining = await list_my_portfolio(profile=profile)
    assert remaining == []


async def test_profile_detail_embeds_services_and_portfolio():
    profile = await make_artisan_with_profile()
    await create_service(
        ServiceCreate(name="Leak Repair", category="plumbing", pricing_type="fixed", price=3000),
        profile=profile,
    )
    await create_portfolio_item(
        PortfolioItemCreate(
            title="Bathroom job", category="plumbing", image_url="https://example.com/a.jpg"
        ),
        profile=profile,
    )

    detail = await get_profile_detail(str(profile.id))
    assert detail.business_name == "Ade Plumbing Co"
    assert len(detail.services) == 1
    assert len(detail.portfolio) == 1
    assert detail.reviews == []


async def test_gig_view_counter_increments_on_fetch():
    profile = await make_artisan_with_profile()
    created = await create_gig(
        GigCreate(
            title="Custom shelving",
            description="Built-in shelves",
            category="carpentry",
            price=15000,
            delivery_time_days=3,
        ),
        profile=profile,
    )

    fetched_once = await get_gig(created.id)
    fetched_twice = await get_gig(created.id)
    assert fetched_twice.id == created.id

    from app.models.gig import Gig

    gig_doc = await Gig.get(created.id)
    assert gig_doc.views_count == 2
