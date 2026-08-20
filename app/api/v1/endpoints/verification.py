# app/api/v1/endpoints/verification.py
from datetime import datetime
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.models.profile import Profile
from app.models.user import User
from app.models.verification import (
    Verification,
    VerificationResponse,
    VerificationReview,
    VerificationStatus,
    VerificationSubmit,
)

router = APIRouter()


def build_verification_response(v: Verification) -> VerificationResponse:
    user_id = str(v.user.ref.id if hasattr(v.user, "ref") else v.user.id)
    return VerificationResponse(
        id=str(v.id),
        user_id=user_id,
        nin=v.nin,
        id_type=v.id_type,
        id_number=v.id_number,
        id_card_image=v.id_card_image,
        selfie_image=v.selfie_image,
        status=v.status,
        rejection_reason=v.rejection_reason,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


@router.post("/submit", response_model=VerificationResponse, status_code=status.HTTP_201_CREATED)
async def submit_verification(
    verification_in: VerificationSubmit,
    current_user: User = Depends(get_current_user),
):
    """Submit identity verification documents."""
    existing_verification = await Verification.find_one({"user.$id": current_user.id})

    if existing_verification:
        if existing_verification.status == VerificationStatus.APPROVED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your identity has already been verified.",
            )
        if existing_verification.status == VerificationStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your previous verification request is still pending review.",
            )

        # Allow resubmission if previously rejected
        await existing_verification.set(
            {
                "nin": verification_in.nin,
                "id_type": verification_in.id_type,
                "id_number": verification_in.id_number,
                "id_card_image": verification_in.id_card_image,
                "selfie_image": verification_in.selfie_image,
                "status": VerificationStatus.PENDING,
                "rejection_reason": None,
                "updated_at": datetime.utcnow(),
            }
        )
        return build_verification_response(existing_verification)

    verification = Verification(
        user=current_user,
        **verification_in.model_dump(),
    )
    await verification.insert()
    return build_verification_response(verification)


@router.get("/status", response_model=VerificationResponse)
async def get_verification_status(
    current_user: User = Depends(get_current_user),
):
    """Check identity verification status for the logged-in user."""
    verification = await Verification.find_one({"user.$id": current_user.id})
    if not verification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No verification request submitted yet.",
        )
    return build_verification_response(verification)


@router.patch("/{verification_id}/review", response_model=VerificationResponse)
async def review_verification(
    verification_id: str,
    review_in: VerificationReview,
):
    """Admin endpoint to approve or reject verification requests."""
    verification = await Verification.get(PydanticObjectId(verification_id))
    if not verification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Verification request not found.",
        )

    verification.status = review_in.status
    verification.rejection_reason = review_in.rejection_reason
    verification.updated_at = datetime.utcnow()
    await verification.save()

    # If approved, update profile verification status
    user_id = (
        verification.user.ref.id
        if hasattr(verification.user, "ref")
        else verification.user.id
    )
    if review_in.status == VerificationStatus.APPROVED:
        profile = await Profile.find_one({"user.$id": user_id})
        if profile:
            profile.is_verified = True
            await profile.save()

    return build_verification_response(verification)