# app/api/v1/router.py
from fastapi import APIRouter
from app.api.v1.endpoints import (
    account,
    admin,
    auth,
    bookings,
    chat,
    favorites,
    gigs,
    notifications,
    payments,
    portfolio,
    profiles,
    reviews,
    services,
    support,
    verification,
    wallet,
)

api_router = APIRouter()

# Register endpoint routers
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(account.router, prefix="/auth", tags=["Auth"])
api_router.include_router(profiles.router, prefix="/profiles", tags=["Profiles"])
api_router.include_router(services.router, prefix="/profiles/me/services", tags=["Services"])
api_router.include_router(portfolio.router, prefix="/profiles/me/portfolio", tags=["Portfolio"])
api_router.include_router(gigs.router, prefix="/gigs", tags=["Gigs"])
api_router.include_router(verification.router, prefix="/verification", tags=["Verification"])
api_router.include_router(chat.router, prefix="/chat", tags=["Chat"])
api_router.include_router(chat.conversations_router, prefix="/conversations", tags=["Chat"])
api_router.include_router(bookings.router, prefix="/bookings", tags=["Bookings"])
api_router.include_router(wallet.router, prefix="/wallet", tags=["Wallet"])
api_router.include_router(reviews.router, prefix="/reviews", tags=["Reviews"])
api_router.include_router(favorites.router, prefix="/favorites", tags=["Favorites"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])
api_router.include_router(admin.router, prefix="/admin", tags=["Admin"])
api_router.include_router(payments.router, prefix="/payments", tags=["Payments"])
api_router.include_router(support.router, prefix="/support", tags=["Support"])
