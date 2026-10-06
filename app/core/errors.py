# app/core/errors.py
"""Error responses the frontend can branch on.

`APIError` adds a stable machine-readable `code` next to the human
`detail`, e.g. {"detail": "Enter your 2FA code to continue.",
"code": "totp_required"} (frontend ask 41). Plain HTTPExceptions keep the
usual {"detail": ...} shape.

`CatchAllErrorsMiddleware` turns any unhandled exception into a JSON 500.
It sits *inside* the CORS middleware, so the browser still gets CORS
headers on the error and the app sees a real response instead of what
looks like a dropped connection (frontend ask 33).
"""
import logging
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("kazihub.errors")


class APIError(HTTPException):
    def __init__(self, status_code: int, detail: str, code: str, headers: Optional[dict] = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
        headers=exc.headers,
    )


class CatchAllErrorsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception:
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            return JSONResponse(
                status_code=500,
                content={"detail": "Something went wrong on our side. Please try again.", "code": "server_error"},
            )
