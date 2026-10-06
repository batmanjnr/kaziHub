# app/models/profile.py
from datetime import datetime
from typing import List, Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field
from pymongo import IndexModel
from typing import Literal

from pydantic import model_validator

from app.core.constants import PHONE_VISIBILITY_OPTIONS  # noqa: F401  (re-exported)
from app.core.validators import (
    Bio,
    Category,
    Neighborhood,
    PhoneVisibility,
    ResponseTime,
    Skills,
    Tagline,
    YearsOfExperience,
)
from app.models.user import User

PRICING_TYPES = ("fixed", "starting", "quote_required")
AVAILABILITY_STATUSES = ("Available", "Busy", "Offline")
PricingType = Literal["fixed", "starting", "quote_required"]
AvailabilityStatus = Literal["Available", "Busy", "Offline"]


class Profile(Document):
    user: Link[User]
    business_name: Optional[str] = None
    category: str
    tagline: Optional[str] = None
    skills: List[str] = Field(default_factory=list)
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
    # Availability, one meaning per field (ask 4):
    #   is_available        — the artisan is accepting new work (their toggle)
    #   is_paused           — account frozen; hidden from search entirely
    #   availability_status — online right now: Available | Busy | Offline
    #   is_available_now    — derived: availability_status == "Available"
    is_available: bool = True
    is_available_now: bool = False
    availability_status: str = "Offline"  # one of AVAILABILITY_STATUSES
    is_verified: bool = False
    rating_average: float = Field(default=0.0, ge=0, le=5)
    review_count: int = 0
    completed_jobs_count: int = 0
    response_time: Optional[str] = None
    insurance_backed: bool = False
    phone_visibility: str = "after_escrow"  # one of PHONE_VISIBILITY_OPTIONS
    # Whether the neighborhood field is shown on the public profile/search
    # results, separate from phone_visibility.
    share_neighborhood: bool = True
    # Denormalized from User.is_paused so search can filter with a single
    # collection query; kept in sync by the freeze-me/unfreeze-me endpoints.
    is_paused: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

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


def _check_base_price(pricing_type, base_price):
    if pricing_type is not None and base_price is not None:
        if pricing_type != "quote_required" and base_price <= 0:
            raise ValueError("base_price must be greater than 0 unless pricing_type is 'quote_required'.")


class ProfileCreate(BaseModel):
    business_name: Optional[str] = Field(default=None, max_length=80)
    category: Category
    tagline: Optional[Tagline] = None
    skills: Skills = Field(default_factory=list)
    bio: Optional[Bio] = None
    hourly_rate: Optional[float] = Field(default=None, ge=0)
    base_price: Optional[float] = Field(default=None, ge=0)
    pricing_type: Optional[PricingType] = None
    years_of_experience: YearsOfExperience = 0
    address: Optional[str] = Field(default=None, max_length=200)
    city: Optional[str] = Field(default=None, max_length=60)
    neighborhood: Optional[Neighborhood] = None
    response_time: Optional[ResponseTime] = None

    @model_validator(mode="after")
    def _price_rule(self):
        _check_base_price(self.pricing_type, self.base_price)
        return self


class ProfileUpdate(BaseModel):
    business_name: Optional[str] = Field(default=None, max_length=80)
    category: Optional[Category] = None
    tagline: Optional[Tagline] = None
    skills: Optional[Skills] = None
    bio: Optional[Bio] = None
    hourly_rate: Optional[float] = Field(default=None, ge=0)
    base_price: Optional[float] = Field(default=None, ge=0)
    pricing_type: Optional[PricingType] = None
    years_of_experience: Optional[YearsOfExperience] = None
    address: Optional[str] = Field(default=None, max_length=200)
    city: Optional[str] = Field(default=None, max_length=60)
    neighborhood: Optional[Neighborhood] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    is_available: Optional[bool] = None
    availability_status: Optional[AvailabilityStatus] = None
    phone_visibility: Optional[PhoneVisibility] = None
    share_neighborhood: Optional[bool] = None
    insurance_backed: Optional[bool] = None
    response_time: Optional[ResponseTime] = None

    @model_validator(mode="after")
    def _price_rule(self):
        _check_base_price(self.pricing_type, self.base_price)
        return self


class ProfileResponse(BaseModel):
    id: str
    user_id: str
    # Copied from the owning user record (ask 1).
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    profile_picture: Optional[str] = None
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
    is_available: bool = Field(description="Accepting new work (the artisan's own toggle).")
    is_available_now: bool = Field(
        default=False, description="Online right now; same as availability_status == 'Available'."
    )
    availability_status: str = "Offline"
    is_verified: bool
    rating_average: float = 0.0
    review_count: int = 0
    completed_jobs_count: int = 0
    response_time: Optional[str] = None
    insurance_backed: bool = False
    phone_visibility: str = Field(
        default="after_escrow",
        description="after_escrow: shared with a client once their booking is paid into escrow; "
        "verified_only: as after_escrow, and only if that client's ID is verified; hidden: never shared.",
    )
    share_neighborhood: bool = True
    is_paused: bool = Field(default=False, description="Account frozen; never true in public listings.")
