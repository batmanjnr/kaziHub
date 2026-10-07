# app/models/favorite.py
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field
from pymongo import IndexModel

from app.models.user import User


class SavedProfessional(Document):
    user: Link[User]
    artisan: Link[User]
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "saved_professionals"
        indexes = [
            IndexModel([("user", 1), ("artisan", 1)], unique=True),
        ]


class FavoriteResponse(BaseModel):
    artisan_id: str
    artisan_profile_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    business_name: Optional[str] = None
    category: Optional[str] = None
    rating_average: Optional[float] = None
    avatar_url: Optional[str] = None
    created_at: datetime
