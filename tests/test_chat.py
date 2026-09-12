import time

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.chat import (
    get_conversation_messages,
    mark_conversation_read,
    mark_messages_read,
    send_message,
    upload_chat_media,
)
from app.core.security import get_password_hash
from app.core.ws_ticket import WebSocketTicketStore
from app.models.chat import Conversation, Message, MessageCreate
from app.models.user import User

# pytest.ini sets asyncio_mode = auto, so async def tests below run without
# an explicit marker; the sync ws_ticket tests are left unmarked.


async def make_user(email, role="client") -> User:
    user = User(
        first_name="A",
        last_name="B",
        email=email,
        phone_number=f"+234800000{abs(hash(email)) % 10000:04d}",
        state="Lagos",
        role=role,
        hashed_password=get_password_hash("Passw0rd!"),
    )
    await user.insert()
    return user


def test_ws_ticket_is_single_use():
    store = WebSocketTicketStore()
    ticket = store.issue("user-123")

    assert store.redeem(ticket) == "user-123"
    # Second redemption of the same ticket must fail — single use.
    assert store.redeem(ticket) is None


def test_ws_ticket_expires():
    store = WebSocketTicketStore()
    ticket = store.issue("user-123")
    store._tickets[ticket] = ("user-123", time.time() - 1)  # force expiry

    assert store.redeem(ticket) is None


def test_ws_ticket_unknown_is_rejected():
    store = WebSocketTicketStore()
    assert store.redeem("not-a-real-ticket") is None


async def test_send_message_and_mark_read_flow():
    client = await make_user("client@example.com")
    artisan = await make_user("artisan@example.com", role="artisan")
    conv = Conversation(client=client, artisan=artisan)
    await conv.insert()

    sent = await send_message(
        str(conv.id),
        MessageCreate(conversation_id=str(conv.id), content="Hello, when can you come?"),
        current_user=client,
    )
    assert sent.status == "sent"
    assert sent.recipient_id == str(artisan.id)

    messages = await get_conversation_messages(str(conv.id), current_user=artisan)
    assert len(messages) == 1
    assert messages[0].read_at is None

    await mark_messages_read(str(conv.id), current_user=artisan)

    refreshed = await Message.get(sent.id)
    assert refreshed.status == "read"
    assert refreshed.read_at is not None


async def test_mark_read_does_not_touch_own_messages():
    client = await make_user("c2@example.com")
    artisan = await make_user("a2@example.com", role="artisan")
    conv = Conversation(client=client, artisan=artisan)
    await conv.insert()

    await send_message(
        str(conv.id), MessageCreate(conversation_id=str(conv.id), content="hi"), current_user=client
    )

    # The sender marking "read" on their own conversation shouldn't flip
    # the status of their own outgoing message.
    await mark_conversation_read(str(conv.id), str(client.id))

    msgs = await Message.find(Message.conversation_id == str(conv.id)).to_list()
    assert msgs[0].status == "sent"


async def test_upload_chat_media_rejects_disallowed_content_type():
    class FakeUploadFile:
        content_type = "application/x-msdownload"

    client = await make_user("c3@example.com")

    with pytest.raises(HTTPException) as exc_info:
        await upload_chat_media(file=FakeUploadFile(), current_user=client)
    assert exc_info.value.status_code == 400
