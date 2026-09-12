# app/services/moderation.py
"""Pluggable content moderation (spec §9): a basic NSFW/illegal-content
screen before an upload is marked visible in chat, portfolios, gigs, or
profile pictures. No moderation provider is configured for this project yet
— Cloudinary's AI Moderation add-on, AWS Rekognition, and Google Vision
SafeSearch are all viable options. The default implementation approves
everything; swap `moderate_image`'s body for a real provider call, and
every caller below stays the same.
"""
import logging

logger = logging.getLogger("kazihub.moderation")


async def moderate_image(url: str) -> bool:
    """Returns True if the image is safe to show publicly."""
    logger.info("[moderation stub] approved url=%s", url)
    return True
