from datetime import datetime
from typing import Annotated, Dict, List, Literal, Optional
from beanie import Document, Indexed, Link
from app.core.time import utc_now
from pydantic import BaseModel, Field
from app.models.user import User

MESSAGE_STATUSES = ("sending", "sent", "delivered", "read")


class Conversation(Document):
    client: Link[User]
    artisan: Link[User]
    active_booking_id: Optional[str] = None
    active_job_title: Optional[str] = None
    active_job_amount: Optional[float] = None
    last_message: Optional[str] = None
    # Per-user "delete conversation" (ask 36): ids of users who've hidden
    # it (cleared on the next new message), and when each user last cleared
    # it — messages older than that stay hidden from them for good.
    hidden_for: List[str] = Field(default_factory=list)
    cleared_at: Dict[str, datetime] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "conversations"


class Message(Document):
    conversation_id: Indexed(str)
    sender_id: str
    recipient_id: Optional[str] = None
    content: Optional[str] = None
    attachments: List[str] = Field(default_factory=list)
    audio_url: Optional[str] = None
    media_type: Optional[str] = None  # "image" | "video" | "audio", describing attachments/audio_url
    audio_duration: Optional[int] = None  # seconds
    audio_wave_data: List[float] = Field(default_factory=list)
    location_data: Optional[dict] = None  # {lat, lng, addressName, landmark}
    message_type: str = "text"  # "text", "image", "voice", "quote_offer", "booking_update", "location"
    quote_data: Optional[dict] = None
    status: str = "sent"  # one of MESSAGE_STATUSES
    read_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "messages"


# Types a user may send. "quote_offer" and "booking_update" also appear in
# history, but only the server writes those.
UserMessageType = Literal["text", "image", "audio", "voice", "video", "location"]


class MessageCreate(BaseModel):
    # Optional: the REST route and the WebSocket both take the conversation
    # from the URL; if sent, it's ignored.
    conversation_id: Optional[str] = None
    content: Optional[str] = Field(default=None, max_length=4000)
    attachments: List[str] = Field(default_factory=list, max_length=10)
    audio_url: Optional[str] = None
    media_type: Optional[Literal["image", "video", "audio"]] = None
    audio_duration: Optional[int] = Field(default=None, ge=0, le=3600)
    # Stored and returned unchanged (ask 39): the app sends 40 values in 0..1.
    audio_wave_data: List[Annotated[float, Field(ge=0, le=1)]] = Field(default_factory=list, max_length=200)
    location_data: Optional[dict] = None
    message_type: UserMessageType = "text"


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    sender_id: str
    recipient_id: Optional[str] = None
    content: Optional[str] = None
    attachments: List[str] = Field(default_factory=list)
    audio_url: Optional[str] = None
    media_type: Optional[str] = None
    audio_duration: Optional[int] = None
    audio_wave_data: List[float] = Field(default_factory=list)
    location_data: Optional[dict] = None
    message_type: str
    quote_data: Optional[dict] = None
    status: str = "sent"
    read_at: Optional[datetime] = None
    created_at: datetime
