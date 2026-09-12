import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.favorites import list_favorites, save_professional, unsave_professional
from app.api.v1.endpoints.notifications import (
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)
from app.api.v1.endpoints.reviews import create_review, get_reviews_for_professional
from app.core.security import get_password_hash
from app.models.booking import Booking, BookingStatus
from app.models.favorite import SavedProfessional
from app.models.notification import Notification
from app.models.profile import Profile
from app.models.review import Review, ReviewCreate
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def make_user(email, role="client") -> User:
    user = User(
        first_name="A",
        last_name="B",
        email=email,
        phone_number=f"+234800000{abs(hash(email)) % 10000:04d}",
        state="Lagos",
        role=role,
        hashed_password=get_password_hash("Passw0rd!"),
    )
    await user.insert()
    return user


async def make_paid_out_booking(client, artisan) -> Booking:
    booking = Booking(client=client, artisan=artisan, title="Fix sink", amount=5000, status=BookingStatus.PAID_OUT)
    await booking.insert()
    return booking


# --- Reviews ---

async def test_create_review_updates_profile_rating_and_notifies_artisan():
    client = await make_user("rc1@example.com")
    artisan = await make_user("ra1@example.com", role="artisan")
    profile = Profile(user=artisan, category="plumbing", state="Lagos", rating_average=4.0, review_count=1)
    await profile.insert()
    booking = await make_paid_out_booking(client, artisan)

    review = await create_review(
        ReviewCreate(booking_id=str(booking.id), rating=5, comment="Great job"), current_user=client
    )
    assert review.rating == 5

    refreshed_profile = await Profile.get(profile.id)
    assert refreshed_profile.review_count == 2
    assert refreshed_profile.rating_average == 4.5  # (4.0*1 + 5) / 2

    notifications = await Notification.find({"user.$id": artisan.id}).to_list()
    assert len(notifications) == 1
    assert notifications[0].type == "new_review"


async def test_cannot_review_non_completed_booking():
    client = await make_user("rc2@example.com")
    artisan = await make_user("ra2@example.com", role="artisan")
    booking = Booking(client=client, artisan=artisan, title="Fix sink", amount=5000, status=BookingStatus.IN_PROGRESS)
    await booking.insert()

    with pytest.raises(HTTPException) as exc_info:
        await create_review(ReviewCreate(booking_id=str(booking.id), rating=5), current_user=client)
    assert exc_info.value.status_code == 400


async def test_cannot_review_same_booking_twice():
    client = await make_user("rc3@example.com")
    artisan = await make_user("ra3@example.com", role="artisan")
    booking = await make_paid_out_booking(client, artisan)

    await create_review(ReviewCreate(booking_id=str(booking.id), rating=4), current_user=client)
    with pytest.raises(HTTPException) as exc_info:
        await create_review(ReviewCreate(booking_id=str(booking.id), rating=3), current_user=client)
    assert exc_info.value.status_code == 400


async def test_only_the_booking_client_can_review():
    client = await make_user("rc4@example.com")
    other_client = await make_user("rc5@example.com")
    artisan = await make_user("ra4@example.com", role="artisan")
    booking = await make_paid_out_booking(client, artisan)

    with pytest.raises(HTTPException) as exc_info:
        await create_review(ReviewCreate(booking_id=str(booking.id), rating=5), current_user=other_client)
    assert exc_info.value.status_code == 403


async def test_get_reviews_for_professional_is_public_and_scoped():
    client = await make_user("rc6@example.com")
    artisan = await make_user("ra5@example.com", role="artisan")
    other_artisan = await make_user("ra6@example.com", role="artisan")
    booking1 = await make_paid_out_booking(client, artisan)
    booking2 = await make_paid_out_booking(client, other_artisan)
    await create_review(ReviewCreate(booking_id=str(booking1.id), rating=5), current_user=client)
    await create_review(ReviewCreate(booking_id=str(booking2.id), rating=3), current_user=client)

    results = await get_reviews_for_professional(str(artisan.id), limit=20, offset=0)
    assert len(results) == 1
    assert results[0].rating == 5


# --- Favorites ---

async def test_save_list_and_unsave_professional():
    client = await make_user("fc1@example.com")
    artisan = await make_user("fa1@example.com", role="artisan")
    await Profile(user=artisan, category="electrical", state="Lagos", business_name="Zap Co").insert()

    await save_professional(str(artisan.id), current_user=client)
    favorites = await list_favorites(current_user=client)
    assert len(favorites) == 1
    assert favorites[0].business_name == "Zap Co"

    await unsave_professional(str(artisan.id), current_user=client)
    favorites_after = await list_favorites(current_user=client)
    assert favorites_after == []


async def test_saving_same_professional_twice_is_idempotent_not_erroring():
    client = await make_user("fc2@example.com")
    artisan = await make_user("fa2@example.com", role="artisan")

    await save_professional(str(artisan.id), current_user=client)
    await save_professional(str(artisan.id), current_user=client)  # should not raise

    saved = await SavedProfessional.find({"user.$id": client.id}).to_list()
    assert len(saved) == 1


async def test_cannot_save_self():
    client = await make_user("fc3@example.com")
    with pytest.raises(HTTPException) as exc_info:
        await save_professional(str(client.id), current_user=client)
    assert exc_info.value.status_code == 400


# --- Notifications ---

async def test_list_mark_read_and_mark_all_read():
    user = await make_user("nc1@example.com")
    n1 = Notification(user=user, type="new_review", title="A", message="msg1")
    n2 = Notification(user=user, type="new_review", title="B", message="msg2")
    await n1.insert()
    await n2.insert()

    unread = await list_notifications(unread_only=True, limit=20, offset=0, current_user=user)
    assert len(unread) == 2

    await mark_notification_read(str(n1.id), current_user=user)
    unread_after_one = await list_notifications(unread_only=True, limit=20, offset=0, current_user=user)
    assert len(unread_after_one) == 1

    await mark_all_notifications_read(current_user=user)
    unread_after_all = await list_notifications(unread_only=True, limit=20, offset=0, current_user=user)
    assert unread_after_all == []


async def test_cannot_mark_another_users_notification_read():
    owner = await make_user("nc2@example.com")
    intruder = await make_user("nc3@example.com")
    n = Notification(user=owner, type="new_review", title="A", message="msg")
    await n.insert()

    with pytest.raises(HTTPException) as exc_info:
        await mark_notification_read(str(n.id), current_user=intruder)
    assert exc_info.value.status_code == 403
