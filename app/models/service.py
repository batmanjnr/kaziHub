# app/models/service.py
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field

from pydantic import model_validator

from app.core.validators import Category, DurationEstimate
from app.models.profile import PricingType, Profile


class Service(Document):
    artisan_profile: Link[Profile]
    name: str
    category: str
    description: Optional[str] = None
    pricing_type: str  # "fixed" | "starting" | "quote_required"
    price: float = 0.0
    duration_estimate: Optional[str] = "1-2 hrs"
    is_active: bool = True
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "artisan_services"
        indexes = ["artisan_profile"]


def _check_price(pricing_type, price):
    if pricing_type is not None and price is not None:
        if pricing_type != "quote_required" and price <= 0:
            raise ValueError("price must be greater than 0 unless pricing_type is 'quote_required'.")


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    category: Category
    description: Optional[str] = Field(default=None, max_length=500)
    pricing_type: PricingType
    price: float = Field(default=0.0, ge=0)
    duration_estimate: Optional[DurationEstimate] = "1-2 hrs"

    @model_validator(mode="after")
    def _price_rule(self):
        _check_price(self.pricing_type, self.price)
        return self


class ServiceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    category: Optional[Category] = None
    description: Optional[str] = Field(default=None, max_length=500)
    pricing_type: Optional[PricingType] = None
    price: Optional[float] = Field(default=None, ge=0)
    duration_estimate: Optional[DurationEstimate] = None
    is_active: Optional[bool] = None

    @model_validator(mode="after")
    def _price_rule(self):
        _check_price(self.pricing_type, self.price)
        return self


class ServiceResponse(BaseModel):
    id: str
    artisan_profile_id: str
    name: str
    category: str
    description: Optional[str] = None
    pricing_type: str
    price: float
    duration_estimate: Optional[str] = None
    is_active: bool
    created_at: datetime
