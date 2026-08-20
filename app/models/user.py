# app/models/user.py
from datetime import datetime
from typing import Optional
from beanie import Document, Indexed
from pydantic import BaseModel, EmailStr


class User(Document):
    first_name: str
    last_name: str
    email: Indexed(EmailStr, unique=True)
    phone_number: str
    nin: Optional[str] = None
    state: str
    role: str  # "client" or "artisan"
    hashed_password: str
    is_active: bool = True
    is_email_verified: bool = False
    otp_code: Optional[str] = None
    otp_expires_at: Optional[datetime] = None
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "users"


class UserCreate(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    password: str
    phone_number: str
    nin: Optional[str] = None
    state: str
    role: str


class UserUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone_number: Optional[str] = None
    state: Optional[str] = None
    nin: Optional[str] = None


class VerifyEmailSchema(BaseModel):
    email: EmailStr
    otp: str


class ResendOTPSchema(BaseModel):
    email: EmailStr


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    user_id: Optional[str] = None


class UserResponse(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: EmailStr
    phone_number: str
    nin: Optional[str] = None
    state: str
    role: str
    is_active: bool
    is_email_verified: bool
    created_at: datetime