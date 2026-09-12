# app/core/ws_ticket.py
"""Short-lived WebSocket auth tickets (spec §5.4/§10).

The client authenticates the WS handshake with a one-time ticket fetched via
a prior authenticated REST call, never a long-lived access token in the
query string — a token there ends up in server access logs and browser
history, and stays valid (up to 15 minutes) even after being captured. A
ticket is single-use and dead within seconds, so by the time anyone reads a
log line containing it, it's already worthless.

Process-local in-memory store, same tradeoff as app.core.rate_limit: correct
for a single instance, not shared across workers. Swap for a Redis-backed
store (short TTL key, GETDEL) once Phase 10 introduces Redis.
"""
import secrets
import time
from typing import Dict, Optional, Tuple

TICKET_TTL_SECONDS = 30


class WebSocketTicketStore:
    def __init__(self):
        self._tickets: Dict[str, Tuple[str, float]] = {}

    def issue(self, user_id: str) -> str:
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (user_id, time.time() + TICKET_TTL_SECONDS)
        return ticket

    def redeem(self, ticket: str) -> Optional[str]:
        """Single-use: valid or not, the ticket is consumed by this call."""
        entry = self._tickets.pop(ticket, None)
        if not entry:
            return None
        user_id, expires_at = entry
        if time.time() > expires_at:
            return None
        return user_id


ws_ticket_store = WebSocketTicketStore()
