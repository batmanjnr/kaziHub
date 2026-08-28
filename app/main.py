from contextlib import asynccontextmanager
import certifi
import dns.resolver
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.router import api_router
from app.core.config import settings
from app.models.booking import Booking
from app.models.chat import Conversation, Message
from app.models.gig import Gig
from app.models.pending_user import PendingUser
from app.models.profile import Profile
from app.models.transaction import Transaction
from app.models.user import User
from app.models.verification import Verification

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
        tlsCAFile=certifi.where()
    )
    database = client[settings.DATABASE_NAME]

    # Initialize Beanie ODM with all document models
    await init_beanie(
        database=database,
        document_models=[
            User,
            Profile,
            Gig,
            Verification,
            PendingUser,
            Conversation,
            Message,
            Booking,
            Transaction,
        ]
    )
    yield
    client.close()


# 1. Instantiate FastAPI
app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan
)

# 2. Mount static directory for uploads
app.mount("/static", StaticFiles(directory="static"), name="static")

# 3. Add CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins for development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 4. Include API Router
app.include_router(api_router, prefix="/api")