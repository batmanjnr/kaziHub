import os
import uuid
from typing import List
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, status
from bson import ObjectId
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.core.security import decode_access_token
from app.core.websocket_manager import manager
from app.models.chat import Conversation, Message, MessageCreate, MessageResponse
from app.models.user import User

router = APIRouter()

UPLOAD_DIR = "static/uploads/chat"
os.makedirs(UPLOAD_DIR, exist_ok=True)


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


# ---------------------------------------------------------
# REAL-TIME WEBSOCKET ENDPOINT
# ---------------------------------------------------------
@router.websocket("/ws/{conversation_id}")
async def websocket_chat_endpoint(websocket: WebSocket, conversation_id: str, token: str):
    """
    WebSocket endpoint for real-time live messaging.
    Connect via: ws://localhost:8000/api/chat/ws/{conversation_id}?token={jwt_token}
    """
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    user_id = payload["sub"]

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
            raw_data = await websocket.receive_text()
            data = MessageCreate.model_validate_json(raw_data)

            msg = Message(
                conversation_id=conversation_id,
                sender_id=user_id,
                content=data.content,
                attachments=data.attachments,
                audio_url=data.audio_url,
                message_type=data.message_type,
            )
            await msg.insert()

            conv.last_message = data.content or f"[{data.message_type} attachment]"
            conv.updated_at = msg.created_at
            await conv.save()

            broadcast_payload = {
                "event": "new_message",
                "message": {
                    "id": str(msg.id),
                    "conversation_id": msg.conversation_id,
                    "sender_id": msg.sender_id,
                    "content": msg.content,
                    "attachments": msg.attachments,
                    "audio_url": msg.audio_url,
                    "message_type": msg.message_type,
                    "quote_data": msg.quote_data,
                    "created_at": msg.created_at.isoformat(),
                }
            }
            await manager.broadcast_to_conversation(conversation_id, broadcast_payload)

    except WebSocketDisconnect:
        manager.disconnect(conversation_id, websocket)


# ---------------------------------------------------------
# REST ENDPOINTS FOR CONVERSATIONS & CHAT HISTORY
# ---------------------------------------------------------
@router.post("/conversations")
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


@router.get("/conversations")
async def get_my_conversations(current_user: User = Depends(get_current_user)):
    """Fetch user's inbox list."""
    conversations = await Conversation.find({
        "$or": [
            {"client.$id": current_user.id},
            {"artisan.$id": current_user.id}
        ]
    }).sort("-updated_at").to_list()

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


@router.get("/messages/{conversation_id}", response_model=List[MessageResponse])
async def get_conversation_messages(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
):
    """Retrieve message history for a conversation."""
    try:
        conv = await Conversation.get(ObjectId(conversation_id))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid conversation ID format.")

    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    c_id = extract_user_id(conv.client)
    a_id = extract_user_id(conv.artisan)
    if str(current_user.id) not in [c_id, a_id]:
        raise HTTPException(status_code=403, detail="Not authorized.")

    messages = await Message.find(Message.conversation_id == conversation_id).sort("created_at").to_list()
    return [
        MessageResponse(
            id=str(m.id),
            conversation_id=m.conversation_id,
            sender_id=m.sender_id,
            content=m.content,
            attachments=m.attachments,
            audio_url=m.audio_url,
            message_type=m.message_type,
            quote_data=m.quote_data,
            created_at=m.created_at,
        )
        for m in messages
    ]


@router.post("/upload-media")
async def upload_chat_media(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload photos or audio voice notes for chat."""
    ext = file.filename.split(".")[-1] if "." in file.filename else "file"
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_name)

    with open(file_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)

    return {"url": f"/static/uploads/chat/{unique_name}"}