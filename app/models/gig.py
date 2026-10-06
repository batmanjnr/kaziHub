# app/models/gig.py
from datetime import datetime
from typing import List, Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field
from app.models.profile import Profile


class Gig(Document):
    # FIX (spec Appendix A #10): was keyed to users(id); now keyed to
    # artisan_profiles(id) like artisan_services and artisan_portfolios, so
    # all artisan-owned catalog entities share one join pattern.
    artisan_profile: Link[Profile]
    title: str
    description: str
    category: str
    tags: List[str] = Field(default_factory=list)
    price: float
    delivery_time_days: int
    images: List[str] = Field(default_factory=list)
    is_active: bool = True
    views_count: int = 0
    orders_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "gigs"
        indexes = ["artisan_profile"]


class GigCreate(BaseModel):
    title: str
    description: str
    category: str
    tags: Optional[List[str]] = Field(default_factory=list)
    price: float
    delivery_time_days: int
    images: Optional[List[str]] = Field(default_factory=list)


class GigUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    price: Optional[float] = None
    delivery_time_days: Optional[int] = None
    images: Optional[List[str]] = None
    is_active: Optional[bool] = None


class GigResponse(BaseModel):
    id: str
    artisan_profile_id: str
    title: str
    description: str
    category: str
    tags: List[str]
    price: float
    delivery_time_days: int
    images: List[str]
    is_active: bool
    created_at: datetime
