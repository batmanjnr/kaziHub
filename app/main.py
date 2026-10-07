from contextlib import asynccontextmanager
import certifi
import dns.resolver
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import APIError, CatchAllErrorsMiddleware, api_error_handler
from app.core.time import utc_now
from app.core.rate_limit import GlobalRateLimitMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
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
from app.models.push_subscription import PushSubscription
from app.models.support_ticket import SupportTicket
from app.models.transaction import Transaction
from app.models.user import User
from app.models.user_role import UserRole
from app.models.verification import Verification
from app.models.webhook_event import ProcessedWebhookEvent

# Monkey-patch for Beanie/Motor compatibility
AsyncIOMotorClient.append_metadata = lambda self, *args, **kwargs: None

# Route DNS queries through public DNS resolvers
dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ["8.8.8.8", "1.1.1.1"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize Motor client with certifi CA bundle
    client = AsyncIOMotorClient(
        settings.MONGODB_URL,
        tlsCAFile=certifi.where(),
        # Keep a handful of connections warm so a request right after an
        # idle period doesn't pay a fresh TLS handshake on top of the
        # round trip to Atlas; cap the pool so a traffic spike can't open
        # unbounded connections.
        minPoolSize=5,
        maxPoolSize=100,
        # zlib is a stdlib codec, no extra dependency — trims wire size
        # for larger query results (lists, profile detail payloads).
        compressors="zlib",
        # Datetimes come back timezone-aware (UTC), so every API response
        # carries an explicit offset, e.g. "2026-09-25T03:28:33Z" (asks 16, 35).
        tz_aware=True,
    )
    database = client[settings.DATABASE_NAME]

    # Initialize Beanie ODM with all document models
    await init_beanie(
        database=database,
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
            SupportTicket,
            PushSubscription,
        ]
    )
    yield
    client.close()


# 1. Instantiate FastAPI
app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan
)

app.add_exception_handler(APIError, api_error_handler)

# 2. Mount static directory for uploads
app.mount("/static", StaticFiles(directory="static"), name="static")

# 2b. Innermost: turn any unhandled error into a JSON 500. Added before
# CORS so the error response still carries CORS headers (frontend ask 33).
app.add_middleware(CatchAllErrorsMiddleware)

# 3. Add CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins for development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3b. Baseline per-IP rate limit across the whole API (spec §3)
app.add_middleware(GlobalRateLimitMiddleware)

# 3c. Baseline security response headers (security-review fix)
app.add_middleware(SecurityHeadersMiddleware)

# 3d. Compress responses over 1KB — cuts transfer time for list/search
# endpoints, which are the largest JSON payloads this API returns.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 4. Include API Router
app.include_router(api_router, prefix="/api/v1")


@app.head("/health", include_in_schema=False)
@app.get("/health", tags=["Health"])
async def health():
    """Liveness check for uptime monitors and the keep-alive ping (ask 6).
    `paystack_mode` says whether payments run on Paystack test or live keys
    (ask 25)."""
    key = settings.PAYSTACK_SECRET_KEY
    mode = "test" if key.startswith("sk_test_") else "live" if key.startswith("sk_live_") else "unconfigured"
    return {"status": "ok", "time": utc_now(), "paystack_mode": mode}
