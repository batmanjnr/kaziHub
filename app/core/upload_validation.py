# app/core/upload_validation.py
"""Server-side upload validation (spec §9): content-type allow-list and
max-size enforcement, applied before any file reaches storage — every
upload endpoint in the app calls this instead of duplicating its own
ad hoc content_type check.
"""
from typing import Optional, Sequence

from fastapi import HTTPException, UploadFile, status

IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
VIDEO_TYPES = ("video/mp4",)
# audio/mp4 (+ m4a/aac aliases) is what Safari, and so every iPhone
# browser, records (ask 39).
AUDIO_TYPES = ("audio/webm", "audio/wav", "audio/mp4", "audio/x-m4a", "audio/m4a", "audio/aac")
CHAT_MEDIA_TYPES = IMAGE_TYPES + VIDEO_TYPES + AUDIO_TYPES

MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024   # 10 MB
MAX_VIDEO_SIZE_BYTES = 50 * 1024 * 1024   # 50 MB
MAX_AUDIO_SIZE_BYTES = 15 * 1024 * 1024   # 15 MB


def _default_max_size(content_type: Optional[str]) -> int:
    if content_type in VIDEO_TYPES:
        return MAX_VIDEO_SIZE_BYTES
    if content_type in AUDIO_TYPES:
        return MAX_AUDIO_SIZE_BYTES
    return MAX_IMAGE_SIZE_BYTES


async def validate_upload(
    file: UploadFile,
    allowed_types: Sequence[str] = IMAGE_TYPES,
    max_size_bytes: Optional[int] = None,
) -> str:
    """Raises 400 if `file` isn't an allowed content-type or exceeds the
    size limit. Call this before the file is handed to any storage
    provider. Returns the content type without parameters."""
    # Browsers often add parameters, e.g. "audio/webm;codecs=opus".
    base_type = (file.content_type or "").split(";")[0].strip().lower()
    if base_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{file.content_type}'. Allowed: {list(allowed_types)}",
        )

    limit = max_size_bytes or _default_max_size(base_type)

    size = file.size
    if size is None:
        # Stream that doesn't report size upfront: measure by reading once,
        # then rewind so the actual upload still sees the full content.
        content = await file.read()
        size = len(content)
        await file.seek(0)

    if size > limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File too large ({size} bytes). Max allowed: {limit} bytes.",
        )
    return base_type
