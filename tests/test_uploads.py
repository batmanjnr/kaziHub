import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.gigs import upload_gig_image
from app.core.cloudinary import generate_signed_kyc_url
from app.core.security import get_password_hash
from app.core.upload_validation import IMAGE_TYPES, MAX_IMAGE_SIZE_BYTES, validate_upload
from app.models.user import User
from app.services.moderation import moderate_image

# pytest.ini sets asyncio_mode = auto; the one sync test below is unmarked.


class FakeUploadFile:
    def __init__(self, content_type: str, size: int):
        self.content_type = content_type
        self.size = size

    async def read(self):
        return b"x" * self.size

    async def seek(self, offset):
        pass


async def make_artisan(email) -> User:
    user = User(
        first_name="A",
        last_name="B",
        email=email,
        phone_number=f"+234800000{abs(hash(email)) % 10000:04d}",
        state="Lagos",
        role="artisan",
        hashed_password=get_password_hash("Passw0rd!"),
    )
    await user.insert()
    return user


async def test_validate_upload_rejects_disallowed_content_type():
    file = FakeUploadFile(content_type="application/x-msdownload", size=100)
    with pytest.raises(HTTPException) as exc_info:
        await validate_upload(file, allowed_types=IMAGE_TYPES)
    assert exc_info.value.status_code == 400


async def test_validate_upload_rejects_oversized_file():
    file = FakeUploadFile(content_type="image/jpeg", size=MAX_IMAGE_SIZE_BYTES + 1)
    with pytest.raises(HTTPException) as exc_info:
        await validate_upload(file, allowed_types=IMAGE_TYPES)
    assert exc_info.value.status_code == 400
    assert "too large" in exc_info.value.detail.lower()


async def test_validate_upload_accepts_a_conforming_file():
    file = FakeUploadFile(content_type="image/png", size=1024)
    await validate_upload(file, allowed_types=IMAGE_TYPES)  # should not raise


async def test_moderate_image_stub_approves():
    assert await moderate_image("https://example.com/whatever.jpg") is True


def test_generate_signed_kyc_url_produces_a_time_limited_url():
    url = generate_signed_kyc_url("kazihub/verifications/abc123", format="jpg")
    assert isinstance(url, str)
    assert "expires_at" in url
    assert "signature" in url


async def test_gig_image_upload_now_enforces_allowed_types():
    """gigs.py's upload had NO content-type check at all before Phase 11."""
    artisan = await make_artisan("gigupload@example.com")
    file = FakeUploadFile(content_type="application/pdf", size=100)
    with pytest.raises(HTTPException) as exc_info:
        await upload_gig_image(file=file, current_artisan=artisan)
    assert exc_info.value.status_code == 400
