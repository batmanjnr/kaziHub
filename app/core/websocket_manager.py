import logging
from typing import Dict, List, Optional, Tuple

from fastapi import WebSocket

logger = logging.getLogger("kazihub.ws")


class ConnectionManager:
    def __init__(self):
        # conversation_id -> [(websocket, user_id)]
        self.active_connections: Dict[str, List[Tuple[WebSocket, Optional[str]]]] = {}

    async def connect(self, conversation_id: str, websocket: WebSocket, user_id: Optional[str] = None):
        await websocket.accept()
        self.active_connections.setdefault(conversation_id, []).append((websocket, user_id))

    def disconnect(self, conversation_id: str, websocket: WebSocket):
        conns = self.active_connections.get(conversation_id)
        if not conns:
            return
        self.active_connections[conversation_id] = [c for c in conns if c[0] is not websocket]
        if not self.active_connections[conversation_id]:
            del self.active_connections[conversation_id]

    def is_user_connected(self, conversation_id: str, user_id: str) -> bool:
        """True if `user_id` has this conversation open over the socket —
        used to decide between a live "delivered" and a notification."""
        return any(uid == user_id for _, uid in self.active_connections.get(conversation_id, []))

    async def broadcast_to_conversation(self, conversation_id: str, data: dict, exclude: Optional[WebSocket] = None):
        """Broadcast JSON payload to all active participants in a chat room.
        A dead socket is dropped instead of breaking the broadcast."""
        for connection, _ in list(self.active_connections.get(conversation_id, [])):
            if connection is exclude:
                continue
            try:
                await connection.send_json(data)
            except Exception:
                logger.info("Dropping dead websocket in conversation %s", conversation_id)
                self.disconnect(conversation_id, connection)


manager = ConnectionManager()
