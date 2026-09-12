import asyncio
import time
import cloudinary
import cloudinary.uploader
import cloudinary.api
import cloudinary.utils
from urllib.parse import urlparse
from fastapi import UploadFile
from app.core.config import settings

cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)

async def upload_file_to_cloudinary(file: UploadFile, folder: str) -> str:
    """Uploads a file to Cloudinary with optimizations and returns the secure HTTPS URL."""
    
    # Read the file content
    content = await file.read()
    
    # Use to_thread to run the synchronous upload in a thread
    response = await asyncio.to_thread(
        cloudinary.uploader.upload,
        content,
        folder=folder,
        resource_type="auto",
        transformation=[
            {"width": 1200, "height": 1200, "crop": "limit"},
            {"quality": "auto"},
            {"fetch_format": "auto"}
        ]
    )
    
    return response["secure_url"]

async def upload_private_file_to_cloudinary(file: UploadFile, folder: str) -> dict:
    """Uploads a file to Cloudinary's 'private' delivery type (spec §9: KYC
    files live in a private bucket, never publicly reachable by URL alone).
    Returns identifiers needed to later mint a time-limited signed URL, not
    a usable link — see generate_signed_kyc_url below."""
    content = await file.read()
    response = await asyncio.to_thread(
        cloudinary.uploader.upload,
        content,
        folder=folder,
        resource_type="image",
        type="private",
    )
    return {
        "public_id": response["public_id"],
        "format": response.get("format", "jpg"),
    }


def generate_signed_kyc_url(public_id: str, format: str = "jpg", expires_in_seconds: int = 900) -> str:
    """A signed URL for a private KYC document, expiring in 15 minutes by
    default (spec §9) — for admin review only, never stored or cached."""
    expires_at = int(time.time()) + expires_in_seconds
    return cloudinary.utils.private_download_url(
        public_id, format, resource_type="image", type="private", expires_at=expires_at
    )


async def delete_file_from_cloudinary(public_id_or_url: str):
    """Deletes a file from Cloudinary given its public_id or full URL.

    FIX (live-environment testing): Cloudinary's destroy API doesn't accept
    resource_type="auto" — that value is only meaningful for uploads
    (auto-detect on the way in). Every call to destroy() with "auto"
    previously failed with a 400 from Cloudinary itself, crashing every
    caller (portfolio deletion, profile-picture replacement, moderation-
    reject cleanup) with an unhandled 500. This bug predates this session
    and was only caught by testing against the real Cloudinary API — it
    can't be exercised by the mocked test suite.
    """
    resource_type = "image"
    # If it's a URL, extract both public_id and the resource_type actually
    # embedded in the URL path, rather than guessing.
    if public_id_or_url.startswith("http"):
        # URL structure: https://res.cloudinary.com/<cloud>/<resource_type>/<type>/v<version>/<public_id>
        path = urlparse(public_id_or_url).path
        parts = path.split('/')
        if len(parts) > 2 and parts[2] in ("image", "video", "raw"):
            resource_type = parts[2]
        # Extract public_id, removing extension if present
        public_id = "/".join(parts[5:]).rsplit('.', 1)[0]
    else:
        public_id = public_id_or_url

    await asyncio.to_thread(
        cloudinary.uploader.destroy,
        public_id,
        resource_type=resource_type,
    )
