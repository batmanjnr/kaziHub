# app/models/portfolio.py
from datetime import date, datetime
from typing import Optional
from beanie import Document, Link
from pydantic import BaseModel

from app.models.profile import Profile


class PortfolioItem(Document):
    artisan_profile: Link[Profile]
    title: str
    category: str
    image_url: str
    description: Optional[str] = None
    date_completed: Optional[date] = None
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "artisan_portfolios"
        indexes = ["artisan_profile"]


class PortfolioItemCreate(BaseModel):
    title: str
    category: str
    image_url: str
    description: Optional[str] = None
    date_completed: Optional[date] = None


class PortfolioItemResponse(BaseModel):
    id: str
    artisan_profile_id: str
    title: str
    category: str
    image_url: str
    description: Optional[str] = None
    date_completed: Optional[date] = None
    created_at: datetime
