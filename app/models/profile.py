# app/models/profile.py
from datetime import datetime
from typing import List, Optional
from beanie import Document, Link
from pydantic import BaseModel, Field
from pymongo import IndexModel
from app.models.user import User

PRICING_TYPES = ("fixed", "starting", "quote_required")
AVAILABILITY_STATUSES = ("Available", "Busy", "Offline")
PHONE_VISIBILITY_OPTIONS = ("after_escrow", "verified_only", "hidden")


class Profile(Document):
    user: Link[User]
    business_name: Optional[str] = None
    category: str
    tagline: Optional[str] = None
    skills: List[str] = []
    bio: Optional[str] = None
    hourly_rate: Optional[float] = 0.0
    base_price: float = 0.0
    pricing_type: str = "starting"  # one of PRICING_TYPES
    years_of_experience: int = 0
    address: Optional[str] = None
    city: Optional[str] = None
    neighborhood: Optional[str] = None
    state: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # GeoJSON Point, kept in sync with latitude/longitude by the profile
    # endpoints (2dsphere index below powers "nearby artisans" queries — the
    # Mongo equivalent of the spec's PostGIS geography column).
    geo_location: Optional[dict] = None
    is_available: bool = True
    is_available_now: bool = True
    availability_status: str = "Offline"  # one of AVAILABILITY_STATUSES
    is_verified: bool = False
    rating_average: float = Field(default=0.0, ge=0, le=5)
    review_count: int = 0
    completed_jobs_count: int = 0
    response_time: Optional[str] = None
    insurance_backed: bool = False
    phone_visibility: str = "after_escrow"  # one of PHONE_VISIBILITY_OPTIONS
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "profiles"
        indexes = [
            IndexModel([("geo_location", "2dsphere")]),
            IndexModel([("business_name", "text"), ("bio", "text")]),
        ]


def make_geo_point(latitude: Optional[float], longitude: Optional[float]) -> Optional[dict]:
    if latitude is None or longitude is None:
        return None
    return {"type": "Point", "coordinates": [longitude, latitude]}


class ProfileCreate(BaseModel):
    business_name: Optional[str] = None
    category: str
    tagline: Optional[str] = None
    skills: List[str] = []
    bio: Optional[str] = None
    hourly_rate: Optional[float] = None
    base_price: Optional[float] = None
    pricing_type: Optional[str] = None
    years_of_experience: int = 0
    address: Optional[str] = None
    city: Optional[str] = None
    neighborhood: Optional[str] = None


class ProfileUpdate(BaseModel):
    business_name: Optional[str] = None
    category: Optional[str] = None
    tagline: Optional[str] = None
    skills: Optional[List[str]] = None
    bio: Optional[str] = None
    hourly_rate: Optional[float] = None
    base_price: Optional[float] = None
    pricing_type: Optional[str] = None
    years_of_experience: Optional[int] = None
    address: Optional[str] = None
    city: Optional[str] = None
    neighborhood: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_available: Optional[bool] = None
    availability_status: Optional[str] = None
    phone_visibility: Optional[str] = None
    insurance_backed: Optional[bool] = None
    response_time: Optional[str] = None


class ProfileResponse(BaseModel):
    id: str
    user_id: str
    business_name: Optional[str] = None
    category: str
    tagline: Optional[str] = None
    skills: List[str]
    bio: Optional[str] = None
    hourly_rate: Optional[float] = None
    base_price: float = 0.0
    pricing_type: str = "starting"
    years_of_experience: int
    address: Optional[str] = None
    city: Optional[str] = None
    neighborhood: Optional[str] = None
    state: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_available: bool
    is_available_now: bool = True
    availability_status: str = "Offline"
    is_verified: bool
    rating_average: float = 0.0
    review_count: int = 0
    completed_jobs_count: int = 0
    response_time: Optional[str] = None
    insurance_backed: bool = False
    phone_visibility: str = "after_escrow"
