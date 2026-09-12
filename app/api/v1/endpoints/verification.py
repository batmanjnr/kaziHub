# app/api/v1/endpoints/verification.py
from datetime import datetime
from typing import List
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File

from app.api.deps import get_current_admin, get_current_user
from app.core.encryption import decrypt_str, encrypt_str, mask_tail
from app.models.audit_log import AuditLog
from app.models.notification import Notification
from app.models.profile import Profile
from app.models.user import User
from app.models.verification import (
    Verification,
    VerificationResponse,
    VerificationReview,
    VerificationStatus,
    VerificationSubmit,
)
from app.core.cloudinary import generate_signed_kyc_url, upload_private_file_to_cloudinary
from app.core.upload_validation import IMAGE_TYPES, validate_upload

router = APIRouter()


@router.post("/upload")
async def upload_verification_document(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload a KYC document/selfie to Cloudinary's private delivery type
    (spec §9) — the returned public_id is not a usable URL by itself; pass
    it to POST /verification/submit. Admins (and the applicant) later see
    it via a 15-minute signed URL, never a permanent public link."""
    await validate_upload(file, allowed_types=IMAGE_TYPES)
    result = await upload_private_file_to_cloudinary(file, folder="kazihub/verifications")
    return result


def build_verification_response(v: Verification) -> VerificationResponse:
    user_id = str(v.user.ref.id if hasattr(v.user, "ref") else v.user.id)
    return VerificationResponse(
        id=str(v.id),
        user_id=user_id,
        document_type=v.document_type,
        document_number_masked=mask_tail(decrypt_str(v.document_number_encrypted)),
        document_image_url=generate_signed_kyc_url(v.document_image_public_id, v.document_image_format),
        liveness_selfie_url=generate_signed_kyc_url(v.liveness_selfie_public_id, v.liveness_selfie_format),
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
    if not verification_in.biometric_consent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Biometric consent is required to submit verification.",
        )

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
                "document_type": verification_in.document_type,
                "document_number_encrypted": encrypt_str(verification_in.document_number),
                "document_image_public_id": verification_in.document_image_public_id,
                "document_image_format": verification_in.document_image_format,
                "liveness_selfie_public_id": verification_in.liveness_selfie_public_id,
                "liveness_selfie_format": verification_in.liveness_selfie_format,
                "status": VerificationStatus.PENDING,
                "rejection_reason": None,
                "biometric_consent_given_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
        )
        return build_verification_response(existing_verification)

    verification = Verification(
        user=current_user,
        document_type=verification_in.document_type,
        document_number_encrypted=encrypt_str(verification_in.document_number),
        document_image_public_id=verification_in.document_image_public_id,
        document_image_format=verification_in.document_image_format,
        liveness_selfie_public_id=verification_in.liveness_selfie_public_id,
        liveness_selfie_format=verification_in.liveness_selfie_format,
        biometric_consent_given_at=datetime.utcnow(),
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


@router.get("/queue", response_model=List[VerificationResponse])
async def list_verification_queue(
    review_status: str = Query(default="pending", alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_admin: User = Depends(get_current_admin),
):
    """Admin-only queue of submissions awaiting review (or filtered by any
    other status). Without this there'd be no way to discover verification
    IDs to pass to PATCH /{id}/review."""
    query = {}
    if review_status:
        query["status"] = review_status
    items = (
        await Verification.find(query)
        .sort("created_at")
        .skip(offset)
        .limit(limit)
        .to_list()
    )
    return [build_verification_response(v) for v in items]


@router.patch("/{verification_id}/review", response_model=VerificationResponse)
async def review_verification(
    verification_id: str,
    review_in: VerificationReview,
    current_user: User = Depends(get_current_admin),
):
    """Admin endpoint to approve or reject verification requests. Writes to
    audit_logs (spec §5.5/§2) and notifies the applicant of the outcome.

    NOTE: mandatory TOTP 2FA re-auth for admin actions (spec §2/§3) lands in
    Phase 8 alongside the rest of the admin tooling.
    """
    verification = await Verification.get(PydanticObjectId(verification_id))
    if not verification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Verification request not found.",
        )

    previous_status = verification.status
    verification.status = review_in.status
    verification.rejection_reason = review_in.rejection_reason
    verification.reviewed_by = current_user
    verification.reviewed_at = datetime.utcnow()
    verification.updated_at = datetime.utcnow()
    await verification.save()

    user_id = (
        verification.user.ref.id
        if hasattr(verification.user, "ref")
        else verification.user.id
    )

    await AuditLog(
        actor=current_user,
        action="kyc_approved" if review_in.status == VerificationStatus.APPROVED else "kyc_rejected",
        target_type="verification",
        target_id=str(verification.id),
        reason=review_in.rejection_reason,
        metadata={"previous_status": previous_status, "new_status": review_in.status.value},
    ).insert()

    applicant = await User.get(user_id)
    if applicant:
        if review_in.status == VerificationStatus.APPROVED:
            title, message = "Identity verified", "Your identity verification was approved."
        else:
            title = "Identity verification rejected"
            message = review_in.rejection_reason or "Your identity verification was rejected."
        await Notification(
            user=applicant, type="verification_review", title=title, message=message
        ).insert()

    if review_in.status == VerificationStatus.APPROVED:
        profile = await Profile.find_one({"user.$id": user_id})
        if profile:
            profile.is_verified = True
            await profile.save()

    return build_verification_response(verification)
