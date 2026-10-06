# app/models/uploads.py
"""Declared responses of the upload endpoints (frontend ask 15)."""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class UploadUrlResponse(BaseModel):
    url: str = Field(description="Public HTTPS URL of the uploaded file (Cloudinary).")


class ChatUploadResponse(BaseModel):
    url: str = Field(
        description="URL to send as `attachments[]` (images/video) or `audio_url` (voice notes). "
        "Voice notes are always returned as AAC in an .m4a file, which every browser and phone plays."
    )
    media_type: Literal["image", "video", "audio"]
    original_url: Optional[str] = Field(
        default=None, description="Voice notes only: the file exactly as uploaded."
    )


class KycUploadResponse(BaseModel):
    public_id: str = Field(description="Private file id; send to POST /verification/submit.")
    format: str = Field(description="File format, e.g. 'jpg'; send back with the public_id.")
    upload_token: str = Field(description="Binds this upload to you; send back with the public_id.")
