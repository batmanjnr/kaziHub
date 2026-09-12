# app/models/favorite.py
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from pydantic import BaseModel
from pymongo import IndexModel

from app.models.user import User


class SavedProfessional(Document):
    user: Link[User]
    artisan: Link[User]
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "saved_professionals"
        indexes = [
            IndexModel([("user", 1), ("artisan", 1)], unique=True),
        ]


class FavoriteResponse(BaseModel):
    artisan_id: str
    business_name: Optional[str] = None
    category: Optional[str] = None
    rating_average: Optional[float] = None
    avatar_url: Optional[str] = None
    created_at: datetime
