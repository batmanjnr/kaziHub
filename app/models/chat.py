from datetime import datetime
from typing import List, Optional
from beanie import Document, Link
from pydantic import BaseModel
from app.models.user import User


class Conversation(Document):
    client: Link[User]
    artisan: Link[User]
    active_booking_id: Optional[str] = None
    active_job_title: Optional[str] = None
    active_job_amount: Optional[float] = None
    last_message: Optional[str] = None
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "conversations"


class Message(Document):
    conversation_id: str
    sender_id: str
    content: Optional[str] = None
    attachments: List[str] = []
    audio_url: Optional[str] = None
    message_type: str = "text"  # "text", "image", "voice", "quote_offer", "booking_update"
    quote_data: Optional[dict] = None
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "messages"


class MessageCreate(BaseModel):
    conversation_id: str
    content: Optional[str] = None
    attachments: List[str] = []
    audio_url: Optional[str] = None
    message_type: str = "text"


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    sender_id: str
    content: Optional[str] = None
    attachments: List[str] = []
    audio_url: Optional[str] = None
    message_type: str
    quote_data: Optional[dict] = None
    created_at: datetime