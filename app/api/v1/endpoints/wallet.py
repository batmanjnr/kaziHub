import hmac
import hashlib
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from bson import ObjectId

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.websocket_manager import manager
from app.models.booking import Booking, BookingStatus
from app.models.chat import Conversation
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User

router = APIRouter()


@router.post("/initialize-escrow/{booking_id}")
async def initialize_escrow_payment(
    booking_id: str,
    current_user: User = Depends(get_current_user),
):
    """Initialize Paystack checkout for escrow funding."""
    booking = await Booking.get(ObjectId(booking_id))
    if not booking or str(booking.client.ref.id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    if booking.amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid booking amount.")

    ref = f"KZ-ESCROW-{booking_id[:8]}-{ObjectId()}"
    tx = Transaction(
        user=current_user,
        booking=booking,
        reference=ref,
        amount=booking.amount,
        type=TransactionType.ESCROW_DEPOSIT,
        status=TransactionStatus.PENDING,
    )
    await tx.insert()

    paystack_url = "https://api.paystack.co/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "email": current_user.email,
        "amount": int(booking.amount * 100),
        "reference": ref,
        "metadata": {"booking_id": str(booking.id)},
    }

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(paystack_url, json=payload, headers=headers, timeout=10.0)
            res_data = res.json()

        if res_data.get("status"):
            return {
                "authorization_url": res_data["data"]["authorization_url"],
                "access_code": res_data["data"]["access_code"],
                "reference": ref,
            }
        raise HTTPException(status_code=400, detail=res_data.get("message", "Paystack error"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Payment initialization failed: {e}")


@router.post("/webhook/paystack")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(None)):
    """Paystack webhook to confirm payment and notify chat in real time."""
    body = await request.body()
    hash_sig = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
        body,
        hashlib.sha256
    ).hexdigest()

    if hash_sig != x_paystack_signature:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_data = await request.json()
    if event_data.get("event") == "charge.success":
        data = event_data["data"]
        ref = data["reference"]

        tx = await Transaction.find_one(Transaction.reference == ref)
        if tx and tx.status == TransactionStatus.PENDING:
            tx.status = TransactionStatus.SUCCESSFUL
            tx.gateway_response = data.get("gateway_response")
            await tx.save()

            booking = await Booking.get(tx.booking.ref.id)
            if booking:
                booking.status = BookingStatus.ESCROW_FUNDED
                booking.payment_reference = ref
                await booking.save()

                # Notify chat room over live WebSocket
                conv = await Conversation.find_one({
                    "client.$id": booking.client.ref.id,
                    "artisan.$id": booking.artisan.ref.id
                })
                if conv:
                    await manager.broadcast_to_conversation(str(conv.id), {
                        "event": "escrow_funded",
                        "booking_id": str(booking.id),
                        "amount": booking.amount,
                        "status": booking.status,
                    })

    return {"status": "success"}


@router.post("/release-escrow/{booking_id}")
async def release_escrow_to_artisan(
    booking_id: str,
    current_user: User = Depends(get_current_user),
):
    """Client releases funds to artisan upon job completion."""
    booking = await Booking.get(ObjectId(booking_id))
    if not booking or str(booking.client.ref.id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    if booking.status != BookingStatus.COMPLETED_BY_ARTISAN:
        raise HTTPException(status_code=400, detail="Job must be completed by artisan first.")

    artisan = await User.get(booking.artisan.ref.id)
    payout_tx = Transaction(
        user=artisan,
        booking=booking,
        reference=f"KZ-RELEASE-{booking_id[:8]}-{ObjectId()}",
        amount=booking.amount,
        type=TransactionType.ESCROW_RELEASE,
        status=TransactionStatus.SUCCESSFUL,
    )
    await payout_tx.insert()

    booking.status = BookingStatus.PAID_OUT
    await booking.save()

    conv = await Conversation.find_one({
        "client.$id": booking.client.ref.id,
        "artisan.$id": booking.artisan.ref.id
    })
    if conv:
        await manager.broadcast_to_conversation(str(conv.id), {
            "event": "escrow_released",
            "booking_id": str(booking.id),
            "status": booking.status,
        })

    return {"detail": "Funds released successfully.", "booking_status": booking.status}