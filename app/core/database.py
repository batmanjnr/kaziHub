# app/core/database.py
import certifi
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from app.core.config import settings

# Patch AsyncIOMotorClient for Beanie/Motor compatibility
if not hasattr(AsyncIOMotorClient, "append_metadata"):
    AsyncIOMotorClient.append_metadata = lambda self, *args, **kwargs: None

async def init_db():
    client = AsyncIOMotorClient(
        settings.MONGODB_URL,
        tlsCAFile=certifi.where()
    )
    await init_beanie(
        database=client[settings.DATABASE_NAME],
        document_models=[
            # Models registered here
        ]
    )