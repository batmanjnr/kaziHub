from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect, status
from bson import ObjectId
from pydantic import BaseModel, Field, ValidationError

from app.api.deps import get_current_user
from app.core.cloudinary import delete_file_from_cloudinary, upload_audio_to_cloudinary, upload_file_to_cloudinary
from app.core.errors import APIError
from app.core.time import as_utc, utc_now
from app.core.upload_validation import AUDIO_TYPES, CHAT_MEDIA_TYPES, IMAGE_TYPES, VIDEO_TYPES, validate_upload
from app.core.websocket_manager import manager
from app.core.ws_ticket import TICKET_TTL_SECONDS, ws_ticket_store
from app.models.chat import Conversation, Message, MessageCreate, MessageResponse
from app.models.profile import Profile
from app.models.uploads import ChatUploadResponse
from app.models.user import User
from app.services.moderation import moderate_image
from app.services.notifications import notify

# WebSocket + media-upload utility endpoints, mounted under /chat.
router = APIRouter()
# Conversation/message REST CRUD, mounted at /conversations to match spec §5.4.
conversations_router = APIRouter()


class StartConversationSchema(BaseModel):
    artisan_id: str = Field(description="The artisan's user id (a profile's `user_id`).")


class ConversationResponse(BaseModel):
    id: str
    client_id: str
    artisan_id: str
    client_name: Optional[str] = None
    client_avatar: Optional[str] = None
    artisan_name: Optional[str] = None
    artisan_avatar: Optional[str] = None
    artisan_profile_id: Optional[str] = None
    active_booking_id: Optional[str] = None
    active_job_title: Optional[str] = None
    active_job_amount: Optional[float] = None
    last_message: Optional[str] = None
    unread_count: int = 0
    updated_at: datetime


class WsTicketResponse(BaseModel):
    ticket: str = Field(description="Single-use; pass as `?ticket=` when opening the socket.")
    expires_in: int = Field(description="Seconds until the ticket expires if unused.")
    websocket_path: str = Field(description="Path to open, with {conversation_id} filled in by you.")


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


def _other_participant_id(conv: Conversation, user_id: str) -> str:
    c_id = extract_user_id(conv.client)
    a_id = extract_user_id(conv.artisan)
    return a_id if user_id == c_id else c_id


async def mark_conversation_read(conversation_id: str, reader_id: str) -> None:
    """Marks every message in the conversation not authored by `reader_id`
    as read (spec §10: sending -> sent -> delivered -> read)."""
    await Message.find(
        {
            "conversation_id": conversation_id,
            "sender_id": {"$ne": reader_id},
            "status": {"$ne": "read"},
        }
    ).update({"$set": {"status": "read", "read_at": utc_now()}})


async def _conversation_responses(conversations: List[Conversation], viewer_id: str) -> List[ConversationResponse]:
    """Names and avatars for both people (ask 29), one query each for users
    and artisan profiles."""
    user_ids = set()
    for c in conversations:
        user_ids.add(ObjectId(extract_user_id(c.client)))
        user_ids.add(ObjectId(extract_user_id(c.artisan)))
    users: Dict[str, User] = {
        str(u.id): u for u in await User.find({"_id": {"$in": list(user_ids)}}).to_list()
    }
    artisan_ids = [ObjectId(extract_user_id(c.artisan)) for c in conversations]
    profiles = {
        extract_user_id(p.user): p
        for p in await Profile.find({"user.$id": {"$in": artisan_ids}}).to_list()
    }

    out = []
    for c in conversations:
        c_id, a_id = extract_user_id(c.client), extract_user_id(c.artisan)
        client, artisan, profile = users.get(c_id), users.get(a_id), profiles.get(a_id)
        unread_query = {"conversation_id": str(c.id), "sender_id": {"$ne": viewer_id}, "status": {"$ne": "read"}}
        cleared = c.cleared_at.get(viewer_id)
        if cleared:
            unread_query["created_at"] = {"$gt": cleared}
        out.append(
            ConversationResponse(
                id=str(c.id),
                client_id=c_id,
                artisan_id=a_id,
                client_name=f"{client.first_name} {client.last_name}" if client else None,
                client_avatar=client.profile_picture if client else None,
                artisan_name=f"{artisan.first_name} {artisan.last_name}" if artisan else None,
                artisan_avatar=artisan.profile_picture if artisan else None,
                artisan_profile_id=str(profile.id) if profile else None,
                active_booking_id=c.active_booking_id,
                active_job_title=c.active_job_title,
                active_job_amount=c.active_job_amount,
                last_message=c.last_message,
                unread_count=await Message.find(unread_query).count(),
                updated_at=c.updated_at,
            )
        )
    return out


async def send_chat_message(conv: Conversation, sender_id: str, data: MessageCreate) -> Message:
    """The one send path for REST and the WebSocket.

    Refuses if either person's account is frozen (ask 17), stores the
    message, un-hides the conversation for both people (ask 36), broadcasts
    it, and then either reports it delivered (recipient has the chat open
    live) or notifies the recipient (ask 31).
    """
    conversation_id = str(conv.id)
    recipient_id = _other_participant_id(conv, sender_id)
    sender = await User.get(ObjectId(sender_id))
    recipient = await User.get(ObjectId(recipient_id))
    if sender and sender.is_paused:
        raise APIError(status.HTTP_423_LOCKED, "Your account is frozen.", code="account_frozen")
    if recipient is None or recipient.is_paused or recipient.deleted_at is not None:
        raise APIError(
            status.HTTP_423_LOCKED,
            "This person isn't receiving messages right now.",
            code="recipient_unavailable",
        )
    if not (data.content or data.attachments or data.audio_url or data.location_data):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A message needs content, an attachment, audio or a location.")

    msg = Message(
        conversation_id=conversation_id,
        sender_id=sender_id,
        recipient_id=recipient_id,
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

    conv.last_message = data.content or f"[{data.message_type}]"
    conv.updated_at = msg.created_at
    conv.hidden_for = []
    await conv.save()

    await manager.broadcast_to_conversation(conversation_id, {
        "event": "new_message",
        "message": build_message_response(msg).model_dump(mode="json"),
    })

    if manager.is_user_connected(conversation_id, recipient_id):
        msg.status = "delivered"
        await msg.save()
        await manager.broadcast_to_conversation(conversation_id, {
            "event": "message_delivered",
            "conversation_id": conversation_id,
            "message_id": str(msg.id),
        })
    else:
        preview = (data.content or "").strip()
        if not preview:
            preview = {"image": "Sent a photo", "audio": "Sent a voice note", "voice": "Sent a voice note",
                       "video": "Sent a video", "location": "Shared a location"}.get(data.message_type, "Sent a message")
        await notify(
            recipient,
            "new_message",
            f"New message from {sender.first_name}" if sender else "New message",
            preview[:140],
        )
    return msg


# ---------------------------------------------------------
# WS AUTH TICKET (spec §5.4/§10 fix)
# ---------------------------------------------------------
@router.post("/ws-ticket", response_model=WsTicketResponse)
async def issue_ws_ticket(current_user: User = Depends(get_current_user)):
    """Issue a one-time ticket for the live chat WebSocket, and the full
    socket protocol (ask 32 — FastAPI can't list WebSocket routes in
    /openapi.json, so it's documented here).

    **Connect**

    1. Call this endpoint. The ticket is single-use and expires 30 seconds
       after it's issued, so fetch a new one for every connect/reconnect.
    2. Open `wss://<api-host>/api/v1/chat/ws/{conversation_id}?ticket={ticket}`.

    **Close codes**

    - `1008` before the connection is accepted: ticket missing, expired or
      already used; conversation not found; or you aren't one of its two
      participants. Fetch a new ticket before retrying.
    - `1000`/`1001`: normal close (you or the server shut down). Reconnect
      with a new ticket.
    - Anything else (e.g. `1006`): network drop. Reconnect with a new ticket
      and re-fetch `GET /conversations/{id}/messages` to fill any gap.

    **Client → server** (JSON objects)

    | Action | Body |
    |---|---|
    | send a message | `{"action": "send", "content": "Hi", "message_type": "text"}` — any fields of the REST send body (`content`, `attachments`, `audio_url`, `media_type`, `audio_duration`, `audio_wave_data`, `location_data`, `message_type`). `"action"` may be omitted. |
    | mark read | `{"action": "mark_read"}` |
    | typing | `{"action": "typing", "is_typing": true}` (send `false` when they stop) |
    | keep-alive | `{"action": "ping"}` (e.g. every 25 s) |

    **Server → client** (every frame has `"event"`)

    | Event | Payload |
    |---|---|
    | `new_message` | `{"event": "new_message", "message": <MessageResponse, same as REST>}` — also echoed to the sender |
    | `message_delivered` | `{"event": "message_delivered", "conversation_id": "...", "message_id": "..."}` — the recipient had this chat open |
    | `messages_read` | `{"event": "messages_read", "conversation_id": "...", "reader_id": "..."}` — everything not sent by reader_id is now read |
    | `typing` | `{"event": "typing", "conversation_id": "...", "user_id": "...", "is_typing": true}` — never sent back to the typist |
    | `pong` | `{"event": "pong"}` |
    | `error` | `{"event": "error", "code": "...", "detail": ...}` — the socket stays open. Codes: `invalid_json`, `unknown_action`, `validation_error` (detail is the list of field errors), `account_frozen`, `recipient_unavailable`, `rejected` |
    | `booking_updated` | `{"booking_id", "status"}` — a quote was requested |
    | `quote_received` | `{"message": {id, conversation_id, sender_id, content, message_type: "quote_offer", quote_data: {booking_id, amount, breakdown}, created_at}}` |
    | `quote_accepted` | `{"booking_id", "status"}` |
    | `escrow_funded` | `{"booking_id", "status", "amount"?}` |
    | `escrow_released` | `{"booking_id", "status"}` |
    | `booking_status_changed` | `{"booking_id", "status"}` |

    A message's `status` goes `sent` → `delivered` (if the recipient was
    connected) → `read`. Times are UTC with an offset.
    """
    ticket = ws_ticket_store.issue(str(current_user.id))
    return WsTicketResponse(
        ticket=ticket,
        expires_in=TICKET_TTL_SECONDS,
        websocket_path="/api/v1/chat/ws/{conversation_id}?ticket={ticket}",
    )


# ---------------------------------------------------------
# REAL-TIME WEBSOCKET ENDPOINT
# ---------------------------------------------------------
@router.websocket("/ws/{conversation_id}")
async def websocket_chat_endpoint(websocket: WebSocket, conversation_id: str, ticket: str):
    """
    Live chat for one conversation. FastAPI doesn't list WebSocket routes in
    /openapi.json; the protocol is documented in docs/FRONTEND_API_NOTES.md.

    Connect: wss://<host>/api/v1/chat/ws/{conversation_id}?ticket=<from POST /chat/ws-ticket>
    A bad/expired ticket, unknown conversation, or non-participant is
    closed with code 1008 before accepting.

    Client -> server (JSON):
      {"action": "send", ...MessageCreate fields}   (the "action" key may be omitted)
      {"action": "mark_read"}
      {"action": "typing", "is_typing": true|false}
      {"action": "ping"}

    Server -> client (JSON, all carry "event"):
      new_message        {"message": MessageResponse}
      message_delivered  {"conversation_id", "message_id"}
      messages_read      {"conversation_id", "reader_id"}
      typing             {"conversation_id", "user_id", "is_typing"}
      pong               {}
      error              {"code", "detail"}  (connection stays open)
      plus booking events: booking_updated, quote_received, quote_accepted,
      escrow_funded, escrow_released, booking_status_changed.
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

    await manager.connect(conversation_id, websocket, user_id)

    async def send_error(code: str, detail) -> None:
        await websocket.send_json({"event": "error", "code": code, "detail": detail})

    try:
        while True:
            try:
                raw_data = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                await send_error("invalid_json", "Send JSON objects only.")
                continue
            if not isinstance(raw_data, dict):
                await send_error("invalid_json", "Send JSON objects only.")
                continue

            action = raw_data.pop("action", "send")

            if action == "ping":
                await websocket.send_json({"event": "pong"})
                continue

            if action == "mark_read":
                await mark_conversation_read(conversation_id, user_id)
                await manager.broadcast_to_conversation(conversation_id, {
                    "event": "messages_read",
                    "conversation_id": conversation_id,
                    "reader_id": user_id,
                })
                continue

            if action == "typing":
                await manager.broadcast_to_conversation(conversation_id, {
                    "event": "typing",
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "is_typing": bool(raw_data.get("is_typing", True)),
                }, exclude=websocket)
                continue

            if action != "send":
                await send_error("unknown_action", f"Unknown action '{action}'.")
                continue

            try:
                data = MessageCreate.model_validate(raw_data)
            except ValidationError as e:
                await send_error("validation_error", e.errors(include_url=False, include_context=False))
                continue

            conv = await Conversation.get(conv.id)
            try:
                await send_chat_message(conv, user_id, data)
            except APIError as e:
                await send_error(e.code, e.detail)
            except HTTPException as e:
                await send_error("rejected", e.detail)

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(conversation_id, websocket)


# ---------------------------------------------------------
# CHAT MEDIA UPLOAD
# ---------------------------------------------------------
@router.post("/upload-media", response_model=ChatUploadResponse)
async def upload_chat_media(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload a photo, short video or voice note for chat (asks 15, 39).

    Multipart, one `file` field. Accepted:
      - images: image/jpeg, image/png, image/webp — up to 10 MB
      - video: video/mp4 — up to 50 MB
      - audio: audio/webm, audio/wav, audio/mp4, audio/x-m4a, audio/m4a,
        audio/aac — up to 15 MB

    Voice notes come back as an AAC .m4a `url` that plays on every device
    (Safari's audio/mp4 recording can be uploaded as is). Then send a
    message with `attachments: [url]` (photo/video) or `audio_url: url`.
    """
    content_type = await validate_upload(file, allowed_types=CHAT_MEDIA_TYPES)

    if content_type in AUDIO_TYPES:
        result = await upload_audio_to_cloudinary(file, folder="kazihub/chat")
        return ChatUploadResponse(url=result["url"], media_type="audio", original_url=result["original_url"])

    secure_url = await upload_file_to_cloudinary(file, folder="kazihub/chat")

    # Only images get the NSFW/illegal-content screen — voice notes and
    # short clips aren't in scope for an image classifier (spec §9).
    if content_type in IMAGE_TYPES and not await moderate_image(secure_url):
        await delete_file_from_cloudinary(secure_url)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This image did not pass content moderation.",
        )
    return ChatUploadResponse(url=secure_url, media_type="video" if content_type in VIDEO_TYPES else "image")


# ---------------------------------------------------------
# REST ENDPOINTS FOR CONVERSATIONS & CHAT HISTORY (spec §5.4)
# ---------------------------------------------------------
@conversations_router.post("", response_model=ConversationResponse)
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

    if artisan.is_paused:
        raise APIError(
            status.HTTP_423_LOCKED, "This person isn't receiving messages right now.", code="recipient_unavailable"
        )

    # Standard MongoDB DBRef $id query (avoids aggregation pipeline)
    conv = await Conversation.find_one({
        "client.$id": current_user.id,
        "artisan.$id": artisan.id
    })

    if not conv:
        conv = Conversation(client=current_user, artisan=artisan)
        await conv.insert()
    elif str(current_user.id) in conv.hidden_for:
        conv.hidden_for = [u for u in conv.hidden_for if u != str(current_user.id)]
        await conv.save()

    return (await _conversation_responses([conv], str(current_user.id)))[0]


@conversations_router.get("", response_model=List[ConversationResponse])
async def get_my_conversations(
    # FIX (security review): previously unbounded.
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """Fetch user's inbox list, newest first, with both people's names and
    avatars. Conversations the caller deleted are left out until a new
    message arrives."""
    conversations = (
        await Conversation.find({
            "$or": [
                {"client.$id": current_user.id},
                {"artisan.$id": current_user.id}
            ],
            "hidden_for": {"$ne": str(current_user.id)},
        }).sort("-updated_at").skip(offset).limit(limit).to_list()
    )
    return await _conversation_responses(conversations, str(current_user.id))


@conversations_router.delete("/{conversation_id}", status_code=status.HTTP_200_OK)
async def delete_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
):
    """Delete a conversation for the caller only (ask 36). It leaves their
    inbox and its current messages are cleared for them for good; the
    other person keeps their copy. A new message brings the conversation
    back, showing only messages from then on."""
    conv = await _load_conversation_or_404(conversation_id)
    _assert_participant(conv, current_user)
    me = str(current_user.id)
    if me not in conv.hidden_for:
        conv.hidden_for.append(me)
    conv.cleared_at[me] = utc_now()
    await conv.save()
    return {"detail": "Conversation deleted."}


@conversations_router.get("/{conversation_id}/messages", response_model=List[MessageResponse])
async def get_conversation_messages(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
):
    """Message history, oldest first (ask 35). Messages from before the
    caller last deleted this conversation are left out."""
    conv = await _load_conversation_or_404(conversation_id)
    _assert_participant(conv, current_user)

    query: dict = {"conversation_id": conversation_id}
    cleared = conv.cleared_at.get(str(current_user.id))
    if cleared:
        query["created_at"] = {"$gt": as_utc(cleared)}
    # _id breaks ties: older rows saved before the timestamp fix share one
    # created_at, and _id still preserves their real order.
    messages = await Message.find(query).sort([("created_at", 1), ("_id", 1)]).to_list()
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
    the client isn't currently connected over the socket). `message_type`
    must be one of text, image, audio, voice, video, location (422
    otherwise). 423 `recipient_unavailable` if the other person's account
    is frozen."""
    conv = await _load_conversation_or_404(conversation_id)
    _assert_participant(conv, current_user)

    msg = await send_chat_message(conv, str(current_user.id), payload)
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
