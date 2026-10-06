# app/core/rate_limit.py
"""Fixed-window rate limiting for the auth surface (spec §3).

Process-local in-memory store: correct for a single dev/staging instance,
but resets on restart and isn't shared across workers. Swap `_hits` for a
Redis-backed counter once Phase 10 introduces Redis for background jobs —
the `hit()` interface below won't need to change at call sites.
"""
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings


def get_client_ip(request: Request) -> str:
    """The real client IP. Behind a trusted proxy (Render, a load balancer)
    that's the *right-most* X-Forwarded-For entry — the one the proxy itself
    appended. Entries to its left come from the client and can be forged,
    so trusting the left-most one would let anyone dodge rate limits.
    Without a trusted proxy: the socket peer address."""
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class InMemoryRateLimiter:
    def __init__(self):
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_seconds: int) -> None:
        """Record a hit for `key`; raises 429 if it exceeds `limit` within
        the trailing `window_seconds`."""
        now = time.time()
        window_start = now - window_seconds
        hits = self._hits[key]
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
            )
        hits.append(now)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


rate_limiter = InMemoryRateLimiter()


class GlobalRateLimitMiddleware(BaseHTTPMiddleware):
    """Baseline per-IP limiter across the whole API (spec §3 rule 4): 100
    req/min. Endpoint-specific limits (login, resend-otp, forgot-password)
    are tighter and applied separately inside those handlers."""

    def __init__(self, app, limit: int = None, window_seconds: int = 60):
        super().__init__(app)
        self.limit = limit or settings.GLOBAL_RATE_LIMIT_PER_MINUTE
        self.window_seconds = window_seconds

    async def dispatch(self, request: Request, call_next):
        ip = get_client_ip(request)
        try:
            rate_limiter.hit(f"global:{ip}", self.limit, self.window_seconds)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)
