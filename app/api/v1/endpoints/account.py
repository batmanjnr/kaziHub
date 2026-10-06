# app/api/v1/endpoints/account.py
"""Download My Data (frontend ask 13)."""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.api.v1.endpoints.auth import build_user_response
from app.api.v1.endpoints.bookings import booking_responses
from app.api.v1.endpoints.chat import build_message_response
from app.api.v1.endpoints.gigs import build_gig_response
from app.api.v1.endpoints.profiles import (
    build_portfolio_response,
    build_profile_response,
    build_review_response,
    build_service_response,
)
from app.api.v1.endpoints.support import build_ticket_response
from app.core.encryption import mask_tail
from app.core.time import utc_now
from app.models.bank_account import BankAccount
from app.models.booking import Booking
from app.models.chat import Conversation, Message
from app.models.favorite import SavedProfessional
from app.models.gig import Gig
from app.models.notification import Notification
from app.models.portfolio import PortfolioItem
from app.models.profile import Profile
from app.models.review import Review
from app.models.service import Service
from app.models.support_ticket import SupportTicket
from app.models.transaction import Transaction
from app.models.user import User
from app.models.verification import Verification

router = APIRouter()


@router.get("/me/export")
async def export_my_data(current_user: User = Depends(get_current_user)):
    """Everything KaziHub holds about the caller, as one JSON document:
    account, artisan profile with services, portfolio and gigs, bookings,
    payments, reviews (written and received), saved artisans, messages
    they sent, notifications, support requests, ID-verification status and
    payout account. Secrets (password hash, 2FA secret) are never included,
    and ID and bank numbers are masked. Served as a file download."""
    uid = current_user.id
    data: dict = {
        "exported_at": utc_now().isoformat(),
        "account": build_user_response(current_user).model_dump(mode="json"),
    }

    profile = await Profile.find_one({"user.$id": uid})
    if profile:
        data["artisan_profile"] = build_profile_response(profile, owner=current_user).model_dump(mode="json")
        data["services"] = [
            build_service_response(s).model_dump(mode="json")
            for s in await Service.find({"artisan_profile.$id": profile.id}).to_list()
        ]
        data["portfolio"] = [
            build_portfolio_response(p).model_dump(mode="json")
            for p in await PortfolioItem.find({"artisan_profile.$id": profile.id}).to_list()
        ]
        data["gigs"] = [
            build_gig_response(g).model_dump(mode="json")
            for g in await Gig.find({"artisan_profile.$id": profile.id}).to_list()
        ]

    bookings = await Booking.find(
        {"$or": [{"client.$id": uid}, {"artisan.$id": uid}]}
    ).sort("-created_at").to_list()
    data["bookings"] = [b.model_dump(mode="json") for b in await booking_responses(bookings, current_user)]

    booking_ids = [b.id for b in bookings]
    data["payments"] = [
        {
            "booking_id": str(t.booking.ref.id if hasattr(t.booking, "ref") else t.booking.id),
            "reference": t.transaction_reference,
            "amount": t.amount,
            "type": t.type,
            "status": t.status,
            "created_at": t.created_at.isoformat(),
        }
        for t in await Transaction.find({"booking.$id": {"$in": booking_ids}}).sort("created_at").to_list()
    ]

    data["reviews_written"] = [
        build_review_response(r).model_dump(mode="json")
        for r in await Review.find({"client.$id": uid}).to_list()
    ]
    data["reviews_received"] = [
        build_review_response(r).model_dump(mode="json")
        for r in await Review.find({"artisan.$id": uid}).to_list()
    ]
    data["saved_artisans"] = [
        {
            "artisan_id": str(s.artisan.ref.id if hasattr(s.artisan, "ref") else s.artisan.id),
            "saved_at": s.created_at.isoformat(),
        }
        for s in await SavedProfessional.find({"user.$id": uid}).to_list()
    ]

    conversations = await Conversation.find(
        {"$or": [{"client.$id": uid}, {"artisan.$id": uid}]}
    ).to_list()
    data["messages_sent"] = [
        build_message_response(m).model_dump(mode="json")
        for m in await Message.find(
            {"conversation_id": {"$in": [str(c.id) for c in conversations]}, "sender_id": str(uid)}
        ).sort("created_at").to_list()
    ]
    data["notifications"] = [
        {"type": n.type, "title": n.title, "message": n.message, "is_read": n.is_read,
         "created_at": n.created_at.isoformat()}
        for n in await Notification.find({"user.$id": uid}).sort("-created_at").to_list()
    ]
    data["support_requests"] = [
        build_ticket_response(t).model_dump(mode="json")
        for t in await SupportTicket.find({"user.$id": uid}).to_list()
    ]

    verification = await Verification.find_one({"user.$id": uid})
    if verification:
        data["identity_verification"] = {
            "document_type": verification.document_type,
            "status": verification.status,
            "submitted_at": verification.created_at.isoformat(),
        }
    bank = await BankAccount.find_one({"user.$id": uid})
    if bank:
        data["payout_account"] = {
            "bank_name": bank.bank_name,
            "account_number": mask_tail(bank.account_number),
            "account_name": bank.account_name,
        }

    return JSONResponse(
        content=data,
        headers={"Content-Disposition": 'attachment; filename="kazihub-my-data.json"'},
    )
