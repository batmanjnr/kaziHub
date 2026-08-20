from contextlib import asynccontextmanager
import certifi
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi.middleware.cors import CORSMiddleware
from beanie import init_beanie
import dns.resolver

# Monkey-patch for Beanie/Motor compatibility
AsyncIOMotorClient.append_metadata = lambda self, *args, **kwargs: None

# Route DNS queries through local gateway router to prevent Errno 51
dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ["8.8.8.8", "1.1.1.1"]



from app.core.config import settings
from app.models.user import User
from app.models.gig import Gig
from app.models.verification import Verification
from app.models.profile import Profile
from app.api.v1.router import api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize Motor client with certifi CA bundle
    client = AsyncIOMotorClient(
        settings.MONGODB_URL,
        tlsCAFile=certifi.where()
    )
    database = client[settings.DATABASE_NAME]
    
    # Initialize Beanie ODM
    await init_beanie(
        database=database,
        document_models=[
            User,
            Profile,
            Gig,
            Verification,
        ]
    )
    yield
    client.close()


# 1. Instantiate FastAPI first
app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan
)

# 2. Add CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins for development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Include API Router
app.include_router(api_router, prefix="/api")