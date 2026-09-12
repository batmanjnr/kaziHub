from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect, status
from bson import ObjectId
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.core.cloudinary import delete_file_from_cloudinary, upload_file_to_cloudinary
from app.core.upload_validation import CHAT_MEDIA_TYPES, IMAGE_TYPES, validate_upload
from app.core.websocket_manager import manager
from app.core.ws_ticket import ws_ticket_store
from app.models.chat import Conversation, Message, MessageCreate, MessageResponse
from app.models.user import User
from app.services.moderation import moderate_image

# WebSocket + media-upload utility endpoints, mounted under /chat.
router = APIRouter()
# Conversation/message REST CRUD, mounted at /conversations to match spec §5.4.
conversations_router = APIRouter()


class StartConversationSchema(BaseModel):
    artisan_id: str


def extract_user_id(user_link) -> str:
    """Safely extract user ID string whether the link is fetched or an unfetched DBRef."""
    if not user_link:
        return ""
    if hasattr(user_link, "ref") and user_link.ref:
        return str(user_link.ref.id)
    if hasattr(user_link, "id"):
        return str(user_link.id)
    return str(user_link)


def build_message_response(m: Message) -> MessageResponse:
    return MessageResponse(
        id=str(m.id),
        conversation_id=m.conversation_id,
        sender_id=m.sender_id,
        recipient_id=m.recipient_id,
        content=m.content,
        attachments=m.attachments,
        audio_url=m.audio_url,
        media_type=m.media_type,
        audio_duration=m.audio_duration,
        audio_wave_data=m.audio_wave_data,
        location_data=m.location_data,
        message_type=m.message_type,
        quote_data=m.quote_data,
        status=m.status,
        read_at=m.read_at,
        created_at=m.created_at,
    )


async def _load_conversation_or_404(conversation_id: str) -> Conversation:
    try:
        conv = await Conversation.get(ObjectId(conversation_id))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid conversation ID format.")
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conv


def _assert_participant(conv: Conversation, user: User) -> None:
    c_id = extract_user_id(conv.client)
    a_id = extract_user_id(conv.artisan)
    if str(user.id) not in (c_id, a_id):
        raise HTTPException(status_code=403, detail="Not authorized.")


def _other_participant_id(conv: Conversation, user: User) -> str:
    c_id = extract_user_id(conv.client)
    a_id = extract_user_id(conv.artisan)
    return a_id if str(user.id) == c_id else c_id


async def mark_conversation_read(conversation_id: str, reader_id: str) -> None:
    """Marks every message in the conversation not authored by `reader_id`
    as read (spec §10: sending -> sent -> delivered -> read)."""
    await Message.find(
        {
            "conversation_id": conversation_id,
            "sender_id": {"$ne": reader_id},
            "status": {"$ne": "read"},
        }
    ).update({"$set": {"status": "read", "read_at": datetime.now(timezone.utc)}})


# ---------------------------------------------------------
# WS AUTH TICKET (spec §5.4/§10 fix)
# ---------------------------------------------------------
@router.post("/ws-ticket")
async def issue_ws_ticket(current_user: User = Depends(get_current_user)):
    """Issue a one-time, short-lived ticket for the WS handshake below,
    fetched via this authenticated REST call rather than ever putting the
    real access token in a WS query string."""
    ticket = ws_ticket_store.issue(str(current_user.id))
    return {"ticket": ticket, "expires_in": 30}


# ---------------------------------------------------------
# REAL-TIME WEBSOCKET ENDPOINT
# ---------------------------------------------------------
@router.websocket("/ws/{conversation_id}")
async def websocket_chat_endpoint(websocket: WebSocket, conversation_id: str, ticket: str):
    """
    WebSocket endpoint for real-time live messaging.
    Connect via: ws://localhost:8000/api/v1/chat/ws/{conversation_id}?ticket={one_time_ticket}
    Fetch the ticket first via POST /api/v1/chat/ws-ticket (an authenticated
    REST call) — never pass a long-lived access token here (spec §5.4/§10).
    """
    user_id = ws_ticket_store.redeem(ticket)
    if not user_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        conv = await Conversation.get(ObjectId(conversation_id))
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if not conv:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    c_id = extract_user_id(conv.client)
    a_id = extract_user_id(conv.artisan)
    if user_id not in [c_id, a_id]:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(conversation_id, websocket)

    try:
        while True:
            raw_data = await websocket.receive_json()

            if raw_data.get("action") == "mark_read":
                await mark_conversation_read(conversation_id, user_id)
                await manager.broadcast_to_conversation(conversation_id, {
                    "event": "messages_read",
                    "conversation_id": conversation_id,
                    "reader_id": user_id,
                })
                continue

            data = MessageCreate.model_validate(raw_data)

            msg = Message(
                conversation_id=conversation_id,
                sender_id=user_id,
                recipient_id=a_id if user_id == c_id else c_id,
                content=data.content,
                attachments=data.attachments,
                audio_url=data.audio_url,
                media_type=data.media_type,
                audio_duration=data.audio_duration,
                audio_wave_data=data.audio_wave_data,
                location_data=data.location_data,
                message_type=data.message_type,
                status="sent",
            )
            await msg.insert()

            conv.last_message = data.content or f"[{data.message_type} attachment]"
            conv.updated_at = msg.created_at
            await conv.save()

            await manager.broadcast_to_conversation(conversation_id, {
                "event": "new_message",
                "message": build_message_response(msg).model_dump(mode="json"),
            })

    except WebSocketDisconnect:
        manager.disconnect(conversation_id, websocket)


# ---------------------------------------------------------
# CHAT MEDIA UPLOAD
# ---------------------------------------------------------
@router.post("/upload-media")
async def upload_chat_media(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload a photo or audio voice note for chat to Cloudinary.

    FIX: this used to delete every prior attachment/audio_url in the entire
    conversation on every new upload, destroying chat history — that logic
    is removed. Uploading media does not touch any existing message.
    """
    await validate_upload(file, allowed_types=CHAT_MEDIA_TYPES)
    secure_url = await upload_file_to_cloudinary(file, folder="kazihub/chat")

    # Only images get the NSFW/illegal-content screen — voice notes and
    # short clips aren't in scope for an image classifier (spec §9).
    if file.content_type in IMAGE_TYPES and not await moderate_image(secure_url):
        await delete_file_from_cloudinary(secure_url)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This image did not pass content moderation.",
        )
    return {"url": secure_url}


# ---------------------------------------------------------
# REST ENDPOINTS FOR CONVERSATIONS & CHAT HISTORY (spec §5.4)
# ---------------------------------------------------------
@conversations_router.post("")
async def start_or_get_conversation(
    payload: StartConversationSchema,
    current_user: User = Depends(get_current_user),
):
    """Start or retrieve a chat room with an artisan before booking."""
    try:
        artisan = await User.get(ObjectId(payload.artisan_id))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid artisan ID format."
        )

    if not artisan or artisan.role != "artisan":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artisan not found."
        )

    if str(current_user.id) == str(artisan.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot start a conversation with yourself."
        )

    # Standard MongoDB DBRef $id query (avoids aggregation pipeline)
    conv = await Conversation.find_one({
        "client.$id": current_user.id,
        "artisan.$id": artisan.id
    })

    if not conv:
        conv = Conversation(client=current_user, artisan=artisan)
        await conv.insert()

    return {
        "id": str(conv.id),
        "client_id": extract_user_id(conv.client),
        "artisan_id": extract_user_id(conv.artisan),
        "active_booking_id": conv.active_booking_id,
        "active_job_title": conv.active_job_title,
        "active_job_amount": conv.active_job_amount,
        "last_message": conv.last_message,
        "updated_at": conv.updated_at,
    }


@conversations_router.get("")
async def get_my_conversations(
    # FIX (security review): previously unbounded.
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """Fetch user's inbox list."""
    conversations = (
        await Conversation.find({
            "$or": [
                {"client.$id": current_user.id},
                {"artisan.$id": current_user.id}
            ]
        }).sort("-updated_at").skip(offset).limit(limit).to_list()
    )

    return [
        {
            "id": str(c.id),
            "client_id": extract_user_id(c.client),
            "artisan_id": extract_user_id(c.artisan),
            "active_booking_id": c.active_booking_id,
            "active_job_title": c.active_job_title,
            "active_job_amount": c.active_job_amount,
            "last_message": c.last_message,
            "updated_at": c.updated_at,
        }
        for c in conversations
    ]


@conversations_router.get("/{conversation_id}/messages", response_model=List[MessageResponse])
async def get_conversation_messages(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
):
    """Retrieve message history for a conversation."""
    conv = await _load_conversation_or_404(conversation_id)
    _assert_participant(conv, current_user)

    messages = await Message.find(Message.conversation_id == conversation_id).sort("created_at").to_list()
    return [build_message_response(m) for m in messages]


@conversations_router.post(
    "/{conversation_id}/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED
)
async def send_message(
    conversation_id: str,
    payload: MessageCreate,
    current_user: User = Depends(get_current_user),
):
    """Send a message via REST (in addition to the WS channel — useful when
    the client isn't currently connected over the socket)."""
    conv = await _load_conversation_or_404(conversation_id)
    _assert_participant(conv, current_user)

    msg = Message(
        conversation_id=conversation_id,
        sender_id=str(current_user.id),
        recipient_id=_other_participant_id(conv, current_user),
        content=payload.content,
        attachments=payload.attachments,
        audio_url=payload.audio_url,
        media_type=payload.media_type,
        audio_duration=payload.audio_duration,
        audio_wave_data=payload.audio_wave_data,
        location_data=payload.location_data,
        message_type=payload.message_type,
        status="sent",
    )
    await msg.insert()

    conv.last_message = payload.content or f"[{payload.message_type} attachment]"
    conv.updated_at = msg.created_at
    await conv.save()

    await manager.broadcast_to_conversation(conversation_id, {
        "event": "new_message",
        "message": build_message_response(msg).model_dump(mode="json"),
    })

    return build_message_response(msg)


@conversations_router.patch("/{conversation_id}/read")
async def mark_messages_read(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
):
    """Mark every message in the conversation not sent by the caller as
    read, and notify the other participant live over the socket."""
    conv = await _load_conversation_or_404(conversation_id)
    _assert_participant(conv, current_user)

    await mark_conversation_read(conversation_id, str(current_user.id))
    await manager.broadcast_to_conversation(conversation_id, {
        "event": "messages_read",
        "conversation_id": conversation_id,
        "reader_id": str(current_user.id),
    })
    return {"detail": "Messages marked as read."}
