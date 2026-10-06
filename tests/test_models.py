from datetime import datetime

import pytest

from app.core.encryption import decrypt_str, encrypt_str, mask_tail
from app.models.gig import Gig
from app.models.profile import Profile
from app.models.user import User
from app.models.user_role import UserRole
from app.models.verification import Verification

pytestmark = pytest.mark.asyncio


async def make_user(email="artisan@example.com") -> User:
    user = User(
        first_name="Ade",
        last_name="Bello",
        email=email,
        phone_number="+2348000000000",
        state="Lagos",
        role="artisan",
        hashed_password="hashed",
    )
    await user.insert()
    return user


async def test_user_role_split_allows_dual_capability():
    user = await make_user()
    await UserRole(user=user, role="client").insert()
    await UserRole(user=user, role="artisan").insert()

    roles = await UserRole.find({"user.$id": user.id}).to_list()
    assert {r.role for r in roles} == {"client", "artisan"}


async def test_mutable_default_lists_are_not_shared_between_instances():
    first = User(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        phone_number="+2348000000001",
        state="Lagos",
        role="client",
        hashed_password="hashed",
    )
    second = User(
        first_name="Grace",
        last_name="Hopper",
        email="grace@example.com",
        phone_number="+2348000000002",
        state="Abuja",
        role="artisan",
        hashed_password="hashed",
    )

    first.roles.append("artisan")
    first.roles.append("admin")

    assert second.roles == []
    assert first.roles == ["artisan", "admin"]

    profile_one = Profile(user=first, category="plumbing", state="Lagos")
    profile_two = Profile(user=second, category="electrical", state="Abuja")
    profile_one.skills.append("pipe fitting")

    assert profile_two.skills == []
    assert profile_one.skills == ["pipe fitting"]


async def test_gig_is_keyed_to_artisan_profile_not_user():
    user = await make_user()
    profile = Profile(user=user, category="plumbing", state="Lagos")
    await profile.insert()

    gig = Gig(
        artisan_profile=profile,
        title="Fix a leaking tap",
        description="Quick tap repair",
        category="plumbing",
        price=5000,
        delivery_time_days=1,
    )
    await gig.insert()

    # fetch_links=True requires a $lookup aggregation pipeline stage that
    # mongomock doesn't implement; a plain get() leaves the link unresolved,
    # which is enough to confirm it now points at Profile, not User.
    fetched = await Gig.get(gig.id)
    assert fetched.artisan_profile.ref.id == profile.id
    assert fetched.artisan_profile.ref.collection == "profiles"


async def test_verification_document_number_is_encrypted_at_rest():
    user = await make_user()
    encrypted = encrypt_str("12345678901")

    verification = Verification(
        user=user,
        document_type="nin",
        document_number_encrypted=encrypted,
        document_image_public_id="kazihub/verifications/id123",
        liveness_selfie_public_id="kazihub/verifications/selfie123",
        biometric_consent_given_at=datetime.utcnow(),
    )
    await verification.insert()

    stored = await Verification.get(verification.id)
    assert stored.document_number_encrypted != b"12345678901"
    assert decrypt_str(stored.document_number_encrypted) == "12345678901"
    assert mask_tail(decrypt_str(stored.document_number_encrypted)) == "*******8901"
