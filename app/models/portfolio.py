# app/models/portfolio.py
from datetime import date, datetime
from typing import Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field

from app.core.validators import Category, PastDate
from app.models.profile import Profile


class PortfolioItem(Document):
    artisan_profile: Link[Profile]
    title: str
    category: str
    image_url: str
    description: Optional[str] = None
    date_completed: Optional[date] = None
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "artisan_portfolios"
        indexes = ["artisan_profile"]


class PortfolioItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    category: Category
    image_url: str = Field(min_length=1)
    description: Optional[str] = Field(default=None, max_length=500)
    date_completed: Optional[PastDate] = None


class PortfolioItemUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=80)
    category: Optional[Category] = None
    image_url: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = Field(default=None, max_length=500)
    date_completed: Optional[PastDate] = None


class PortfolioItemResponse(BaseModel):
    id: str
    artisan_profile_id: str
    title: str
    category: str
    image_url: str
    description: Optional[str] = None
    date_completed: Optional[date] = None
    created_at: datetime
