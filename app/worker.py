# app/worker.py
"""Background job scheduler entrypoint (spec §7). Run with:

    python -m app.worker

Uses APScheduler running in-process rather than a Redis-backed queue
(arq/BullMQ) — no Redis instance is provisioned for this project yet.
Swapping to arq later means moving these same job functions into arq task
definitions and running `arq app.worker.WorkerSettings`; the job logic
itself (app/services/jobs/*) doesn't change either way.
"""
import asyncio
import logging

import certifi
import dns.resolver
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.bank_account import BankAccount
from app.models.booking import Booking
from app.models.booking_status_history import BookingStatusHistory
from app.models.chat import Conversation, Message
from app.models.dispute import Dispute
from app.models.favorite import SavedProfessional
from app.models.gig import Gig
from app.models.idempotency_key import IdempotencyRecord
from app.models.notification import Notification
from app.models.pending_user import PendingUser
from app.models.portfolio import PortfolioItem
from app.models.profile import Profile
from app.models.review import Review
from app.models.service import Service
from app.models.session import UserSession
from app.models.transaction import Transaction
from app.models.user import User
from app.models.user_role import UserRole
from app.models.verification import Verification
from app.models.webhook_event import ProcessedWebhookEvent
from app.services.jobs.auto_release import run_auto_release
from app.services.jobs.cleanup import cleanup_expired_otps_and_sessions
from app.services.jobs.notify import dispatch_pending_notifications
from app.services.jobs.webhook_retry import retry_failed_webhooks

logger = logging.getLogger("kazihub.worker")

AsyncIOMotorClient.append_metadata = lambda self, *args, **kwargs: None
dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ["8.8.8.8", "1.1.1.1"]


async def _run_guarded(name: str, coro_fn) -> None:
    """Every scheduled job must alert on failure (spec §7.5) — a silently
    failing auto-release cron means client money sits in limbo with no one
    aware. Standing in for a Slack/PagerDuty webhook: a loud ERROR log line
    that a real alerting integration can tail. Swap this except-branch's
    body for a real alert call; callers don't change."""
    try:
        result = await coro_fn()
        logger.info("job=%s status=ok result=%s", name, result)
    except Exception:
        logger.exception("job=%s status=FAILED", name)


async def main():
    client = AsyncIOMotorClient(settings.MONGODB_URL, tlsCAFile=certifi.where())
    await init_beanie(
        database=client[settings.DATABASE_NAME],
        document_models=[
            User,
            UserRole,
            Profile,
            Service,
            PortfolioItem,
            Gig,
            Verification,
            PendingUser,
            Conversation,
            Message,
            Booking,
            BookingStatusHistory,
            Dispute,
            Review,
            Notification,
            AuditLog,
            SavedProfessional,
            BankAccount,
            Transaction,
            ProcessedWebhookEvent,
            UserSession,
            IdempotencyRecord,
        ],
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_guarded, args=["auto_release", run_auto_release],
        trigger=CronTrigger(minute=0), id="auto_release",
    )
    scheduler.add_job(
        _run_guarded, args=["cleanup", cleanup_expired_otps_and_sessions],
        trigger=CronTrigger(hour=3, minute=0), id="cleanup",
    )
    scheduler.add_job(
        _run_guarded, args=["notify_dispatch", dispatch_pending_notifications],
        trigger=IntervalTrigger(minutes=5), id="notify_dispatch",
    )
    scheduler.add_job(
        _run_guarded, args=["webhook_retry", retry_failed_webhooks],
        trigger=IntervalTrigger(minutes=5), id="webhook_retry",
    )
    scheduler.start()

    logger.info("KaziHub background worker started.")
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown()
        client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
