# app/models/profile.py
from typing import List, Optional
from beanie import Document, Link
from pydantic import BaseModel
from app.models.user import User


class Profile(Document):
    user: Link[User]
    category: str
    skills: List[str] = []
    bio: Optional[str] = None
    hourly_rate: Optional[float] = None
    years_of_experience: int = 0
    address: Optional[str] = None
    city: Optional[str] = None
    state: str
    is_available: bool = True
    is_verified: bool = False

    class Settings:
        name = "profiles"


class ProfileCreate(BaseModel):
    category: str
    skills: List[str]
    bio: Optional[str] = None
    hourly_rate: Optional[float] = None
    years_of_experience: int = 0
    address: Optional[str] = None
    city: Optional[str] = None


class ProfileUpdate(BaseModel):
    category: Optional[str] = None
    skills: Optional[List[str]] = None
    bio: Optional[str] = None
    hourly_rate: Optional[float] = None
    years_of_experience: Optional[int] = None
    address: Optional[str] = None
    city: Optional[str] = None
    is_available: Optional[bool] = None


class ProfileResponse(BaseModel):
    id: str
    user_id: str
    category: str
    skills: List[str]
    bio: Optional[str] = None
    hourly_rate: Optional[float] = None
    years_of_experience: int
    address: Optional[str] = None
    city: Optional[str] = None
    state: str
    is_available: bool
    is_verified: bool