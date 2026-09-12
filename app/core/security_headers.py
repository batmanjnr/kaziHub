# app/core/security_headers.py
"""Baseline security response headers (security-review fix) — previously
entirely absent. This is a pure JSON API (no server-rendered HTML), so a
strict CSP is safe everywhere except FastAPI's own /docs, /redoc, and
/openapi.json, which load their UI from a CDN and would break under it.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if not request.url.path.startswith(_DOCS_PATHS):
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response
