# app/models/user_role.py
"""Capability grants, separate from account identity (see app/models/user.py).

Every user implicitly holds 'client'; a 'client' row should be inserted at
registration. 'artisan' is granted the moment an artisan Profile is created.
Kept as its own collection (rather than folded into `User.roles`) so grant
history (`granted_at`) is preserved and a role can be revoked independently.
"""
from datetime import datetime
from beanie import Document, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field
from pymongo import IndexModel

from app.models.user import User


class UserRole(Document):
    user: Link[User]
    role: str  # "client" | "artisan"
    granted_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "user_roles"
        indexes = [
            IndexModel([("user", 1), ("role", 1)], unique=True),
        ]


class UserRoleResponse(BaseModel):
    role: str
    granted_at: datetime
