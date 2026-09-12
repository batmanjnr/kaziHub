# app/models/audit_log.py
"""Every admin action that touches money or account status must write here in
the same DB transaction as the action itself (spec §2/§4.14)."""
from datetime import datetime
from typing import Optional
from beanie import Document, Link
from pydantic import BaseModel

from app.models.user import User


class AuditLog(Document):
    actor: Link[User]
    action: str  # 'kyc_approved', 'dispute_resolved', 'escrow_force_released', 'user_suspended', ...
    target_type: str  # 'booking' | 'user' | 'dispute' | 'verification'
    target_id: str
    reason: Optional[str] = None
    metadata: Optional[dict] = None
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "audit_logs"
        indexes = ["target_type", "target_id", "actor"]


class AuditLogResponse(BaseModel):
    id: str
    actor_id: str
    action: str
    target_type: str
    target_id: str
    reason: Optional[str] = None
    metadata: Optional[dict] = None
    created_at: datetime
