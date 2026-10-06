"""The live chat socket (frontend ask 32), end to end."""
import pytest
from starlette.testclient import TestClient

from app.core.ws_ticket import ws_ticket_store
from app.main import app
from app.models.chat import Conversation, Message
from tests.http_helpers import make_artisan, make_user

pytestmark = pytest.mark.asyncio


async def test_socket_send_typing_errors_and_delivery():
    client_user = await make_user("wsc@example.com")
    artisan, _ = await make_artisan("wsa@example.com")
    conv = Conversation(client=client_user, artisan=artisan)
    await conv.insert()
    path = f"/api/v1/chat/ws/{conv.id}?ticket="

    # Not `with TestClient(app)`: entering it runs app startup, which would
    # connect to the real database.
    tc = TestClient(app)
    with tc.websocket_connect(path + ws_ticket_store.issue(str(artisan.id))) as artisan_ws, \
            tc.websocket_connect(path + ws_ticket_store.issue(str(client_user.id))) as client_ws:
        client_ws.send_json({"action": "typing", "is_typing": True})
        assert artisan_ws.receive_json() == {
            "event": "typing", "conversation_id": str(conv.id),
            "user_id": str(client_user.id), "is_typing": True,
        }

        client_ws.send_json({"message_type": "not-a-type", "content": "x"})
        assert client_ws.receive_json()["code"] == "validation_error"

        client_ws.send_json({"content": "hello"})
        assert client_ws.receive_json()["event"] == "new_message"
        assert client_ws.receive_json()["event"] == "message_delivered"

        client_ws.send_json({"action": "ping"})
        assert client_ws.receive_json() == {"event": "pong"}

    msg = await Message.find_one({"content": "hello"})
    assert msg.status == "delivered"


async def test_socket_rejects_a_bad_ticket():
    client_user = await make_user("wsb@example.com")
    artisan, _ = await make_artisan("wsba@example.com")
    conv = Conversation(client=client_user, artisan=artisan)
    await conv.insert()
    # Not `with TestClient(app)`: entering it runs app startup, which would
    # connect to the real database.
    tc = TestClient(app)
    with pytest.raises(Exception):
        with tc.websocket_connect(f"/api/v1/chat/ws/{conv.id}?ticket=nope") as ws:
            ws.receive_json()
