# app/services/booking_transitions.py
"""Escrow-safe booking state transitions (spec §6).

Every status change — whether or not it touches escrow — goes through
`apply_booking_transition`, which:

  1. Re-reads the booking and validates the caller's expected current
     status/escrow_status.
  2. Takes an optimistic lock via a single atomic
     `find_one_and_update({"_id": id, "lock_version": v}, ...)`. This is the
     Mongo equivalent of `SELECT ... FOR UPDATE`: if a concurrent writer
     already moved `lock_version`, the filter simply matches nothing, so we
     never blindly overwrite a change we didn't see.
  3. Writes a `booking_status_history` row in the *same* multi-document
     transaction as the status update — no exceptions, including
     cron-driven transitions (Phase 10's auto-release job uses this same
     helper).
  4. Retries a bounded number of times on a lost race rather than failing
     the caller's request outright.

This is what lets the auto-release cron and a client's `POST /dispute` race
on the same booking without either one silently winning: whichever transition
acquires the lock first commits; the other sees a stale `lock_version`,
retries, re-reads the now-updated status, and its own status check fails
cleanly instead of double-transitioning the booking.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable, Optional, Sequence

from beanie import PydanticObjectId
from fastapi import HTTPException, status

from app.models.booking import Booking
from app.models.booking_status_history import BookingStatusHistory
from app.models.user import User

MAX_TRANSITION_RETRIES = 5


class BookingLockConflict(Exception):
    """Internal signal: lost the optimistic-lock race; caller should retry
    from a fresh read rather than reuse stale state."""


@asynccontextmanager
async def _maybe_transaction():
    """A real multi-document transaction against MongoDB Atlas (a replica
    set, so transactions are supported) in production; a no-op passthrough
    under mongomock in tests, which doesn't implement sessions at all.
    Application logic is identical either way — only the cross-document
    atomicity guarantee differs, and single-process tests never race a
    concurrent writer, so nothing here depends on it being real.
    """
    client = Booking.get_pymongo_collection().database.client
    try:
        session = await client.start_session()
    except NotImplementedError:
        yield None
        return

    async with session:
        async with session.start_transaction():
            yield session


ExtraWrites = Callable[[Booking, object], Awaitable[None]]


def _plain(value):
    """Raw pymongo calls bypass Beanie's encoder, so Enum members must be
    unwrapped to their plain value before being written."""
    return value.value if isinstance(value, Enum) else value


async def apply_booking_transition(
    booking_id: PydanticObjectId,
    *,
    allowed_statuses: Optional[Sequence[str]] = None,
    allowed_escrow_statuses: Optional[Sequence[str]] = None,
    updates: Optional[dict] = None,
    to_status: Optional[str] = None,
    changed_by: Optional[User] = None,
    reason: Optional[str] = None,
    extra_writes: Optional[ExtraWrites] = None,
) -> Booking:
    """Atomically move a booking to a new state.

    `extra_writes(booking, session)` runs inside the same transaction after
    the lock is acquired and before commit — used for e.g. inserting a
    Dispute row or an escrow_transactions ledger entry alongside the status
    change.
    """
    updates = dict(updates or {})

    for _ in range(MAX_TRANSITION_RETRIES):
        booking = await Booking.get(booking_id)
        if not booking:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Booking not found.")

        current_status = booking.status.value if hasattr(booking.status, "value") else booking.status
        current_escrow = (
            booking.escrow_status.value
            if hasattr(booking.escrow_status, "value")
            else booking.escrow_status
        )

        if allowed_statuses is not None and current_status not in allowed_statuses:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"Booking status '{current_status}' cannot make this transition.",
            )
        if allowed_escrow_statuses is not None and current_escrow not in allowed_escrow_statuses:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"Escrow status '{current_escrow}' cannot make this transition.",
            )

        write_fields = {k: _plain(v) for k, v in updates.items()}
        if to_status is not None:
            write_fields["status"] = _plain(to_status)
        write_fields["updated_at"] = datetime.now(timezone.utc)

        collection = Booking.get_pymongo_collection()

        try:
            async with _maybe_transaction() as session:
                result = await collection.find_one_and_update(
                    {"_id": booking.id, "lock_version": booking.lock_version},
                    {"$set": write_fields, "$inc": {"lock_version": 1}},
                    session=session,
                    return_document=True,
                )
                if result is None:
                    raise BookingLockConflict()

                history = BookingStatusHistory(
                    booking=booking,
                    from_status=current_status,
                    to_status=to_status or current_status,
                    changed_by=changed_by,
                    reason=reason,
                )
                await history.insert(session=session)

                if extra_writes is not None:
                    await extra_writes(booking, session)
        except BookingLockConflict:
            continue

        return await Booking.get(booking_id)

    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail="This booking is being updated concurrently. Please try again.",
    )
