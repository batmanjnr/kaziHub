# app/models/service.py
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from pydantic import BaseModel

from app.models.profile import Profile


class Service(Document):
    artisan_profile: Link[Profile]
    name: str
    category: str
    description: Optional[str] = None
    pricing_type: str  # "fixed" | "starting" | "quote_required"
    price: float = 0.0
    duration_estimate: Optional[str] = "1-2 hrs"
    is_active: bool = True
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "artisan_services"
        indexes = ["artisan_profile"]


class ServiceCreate(BaseModel):
    name: str
    category: str
    description: Optional[str] = None
    pricing_type: str
    price: float = 0.0
    duration_estimate: Optional[str] = "1-2 hrs"


class ServiceUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    pricing_type: Optional[str] = None
    price: Optional[float] = None
    duration_estimate: Optional[str] = None
    is_active: Optional[bool] = None


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
