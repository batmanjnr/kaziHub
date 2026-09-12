from datetime import datetime
from typing import List, Optional
from beanie import Document, Indexed, Link
from pydantic import BaseModel
from app.models.user import User

MESSAGE_STATUSES = ("sending", "sent", "delivered", "read")


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
    conversation_id: Indexed(str)
    sender_id: str
    recipient_id: Optional[str] = None
    content: Optional[str] = None
    attachments: List[str] = []
    audio_url: Optional[str] = None
    media_type: Optional[str] = None  # "image" | "video" | "audio", describing attachments/audio_url
    audio_duration: Optional[int] = None  # seconds
    audio_wave_data: List[float] = []
    location_data: Optional[dict] = None  # {lat, lng, addressName, landmark}
    message_type: str = "text"  # "text", "image", "voice", "quote_offer", "booking_update", "location"
    quote_data: Optional[dict] = None
    status: str = "sent"  # one of MESSAGE_STATUSES
    read_at: Optional[datetime] = None
    created_at: datetime = datetime.utcnow()

    class Settings:
        name = "messages"


class MessageCreate(BaseModel):
    conversation_id: str
    content: Optional[str] = None
    attachments: List[str] = []
    audio_url: Optional[str] = None
    media_type: Optional[str] = None
    audio_duration: Optional[int] = None
    audio_wave_data: List[float] = []
    location_data: Optional[dict] = None
    message_type: str = "text"


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    sender_id: str
    recipient_id: Optional[str] = None
    content: Optional[str] = None
    attachments: List[str] = []
    audio_url: Optional[str] = None
    media_type: Optional[str] = None
    audio_duration: Optional[int] = None
    audio_wave_data: List[float] = []
    location_data: Optional[dict] = None
    message_type: str
    quote_data: Optional[dict] = None
    status: str = "sent"
    read_at: Optional[datetime] = None
    created_at: datetime
