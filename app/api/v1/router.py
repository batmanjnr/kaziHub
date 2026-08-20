# app/api/v1/router.py
from fastapi import APIRouter
from app.api.v1.endpoints import (
    auth,
    bookings,
    chat,
    gigs,
    portfolio,
    profiles,
    search,
    verification,
    wallet,
)

api_router = APIRouter()

# Register endpoint routers
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(profiles.router, prefix="/profiles", tags=["Profiles"])
api_router.include_router(gigs.router, prefix="/gigs", tags=["Gigs"])
api_router.include_router(verification.router, prefix="/verification", tags=["Verification"])
# api_router.include_router(bookings.router, prefix="/bookings", tags=["Bookings"])
# api_router.include_router(chat.router, prefix="/chat", tags=["Chat"])
# api_router.include_router(gigs.router, prefix="/gigs", tags=["Gigs"])
# api_router.include_router(portfolio.router, prefix="/portfolio", tags=["Portfolio"])
# api_router.include_router(profiles.router, prefix="/profiles", tags=["Profiles"])
# api_router.include_router(search.router, prefix="/search", tags=["Search"])
# api_router.include_router(verification.router, prefix="/verification", tags=["Verification"])
# api_router.include_router(wallet.router, prefix="/wallet", tags=["Wallet"])