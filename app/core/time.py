# app/core/time.py
"""One definition of "now" for the whole app: timezone-aware UTC.

Every stored timestamp comes from here, and the Mongo client is opened with
tz_aware=True, so datetimes read back are aware too. Pydantic then
serializes them with an explicit offset (`...Z`), which is what the
frontend needs to render times correctly (frontend asks 16 and 35).
"""
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Treat a naive datetime (legacy rows) as UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
