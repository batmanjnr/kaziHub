from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.verification import (
    list_verification_queue,
    review_verification,
    submit_verification,
    upload_verification_document,
)
from app.core.kyc_upload_token import sign_kyc_upload
from app.core.security import get_password_hash
from app.models.audit_log import AuditLog
from app.models.notification import Notification
from app.models.profile import Profile
from app.models.user import User
from app.models.verification import VerificationReview, VerificationStatus, VerificationSubmit

pytestmark = pytest.mark.asyncio


async def make_user(email, is_admin=False) -> User:
    user = User(
        first_name="A",
        last_name="B",
        email=email,
        phone_number=f"+234800000{abs(hash(email)) % 10000:04d}",
        state="Lagos",
        role="artisan",
        hashed_password=get_password_hash("Passw0rd!"),
        is_admin=is_admin,
    )
    await user.insert()
    return user


def make_submission(
    user: User,
    doc_public_id: str = "kazihub/verifications/id-a",
    selfie_public_id: str = "kazihub/verifications/selfie-a",
    **overrides,
) -> VerificationSubmit:
    """Builds a VerificationSubmit with valid upload tokens for the given
    user, as if they'd just uploaded via POST /verification/upload."""
    fields = dict(
        document_type="nin",
        document_number="12345678901",
        document_image_public_id=doc_public_id,
        document_image_upload_token=sign_kyc_upload(str(user.id), doc_public_id),
        liveness_selfie_public_id=selfie_public_id,
        liveness_selfie_upload_token=sign_kyc_upload(str(user.id), selfie_public_id),
        biometric_consent=True,
    )
    fields.update(overrides)
    return VerificationSubmit(**fields)


async def test_submit_requires_biometric_consent():
    user = await make_user("kyc1@example.com")
    with pytest.raises(HTTPException) as exc_info:
        await submit_verification(
            make_submission(user, biometric_consent=False),
            current_user=user,
        )
    assert exc_info.value.status_code == 400


async def test_review_approval_writes_audit_log_and_notification_and_verifies_profile():
    applicant = await make_user("kyc2@example.com")
    admin = await make_user("admin1@example.com", is_admin=True)
    profile = Profile(user=applicant, category="plumbing", state="Lagos")
    await profile.insert()

    submitted = await submit_verification(
        make_submission(applicant, document_number="98765432109"),
        current_user=applicant,
    )

    reviewed = await review_verification(
        submitted.id,
        VerificationReview(status=VerificationStatus.APPROVED),
        current_user=admin,
    )
    assert reviewed.status == VerificationStatus.APPROVED

    logs = await AuditLog.find({"target_type": "verification", "target_id": submitted.id}).to_list()
    assert len(logs) == 1
    assert logs[0].action == "kyc_approved"

    notifications = await Notification.find({"user.$id": applicant.id}).to_list()
    assert len(notifications) == 1
    assert notifications[0].type == "verification_review"

    refreshed_profile = await Profile.get(profile.id)
    assert refreshed_profile.is_verified is True


async def test_review_rejection_does_not_verify_profile_but_still_logs():
    applicant = await make_user("kyc3@example.com")
    admin = await make_user("admin2@example.com", is_admin=True)
    profile = Profile(user=applicant, category="plumbing", state="Lagos")
    await profile.insert()

    submitted = await submit_verification(
        make_submission(applicant, document_number="11122233344"),
        current_user=applicant,
    )

    await review_verification(
        submitted.id,
        VerificationReview(status=VerificationStatus.REJECTED, rejection_reason="Blurry photo"),
        current_user=admin,
    )

    refreshed_profile = await Profile.get(profile.id)
    assert refreshed_profile.is_verified is False

    logs = await AuditLog.find({"target_type": "verification", "target_id": submitted.id}).to_list()
    assert logs[0].action == "kyc_rejected"
    assert logs[0].reason == "Blurry photo"


async def test_non_admin_cannot_review():
    applicant = await make_user("kyc4@example.com")
    non_admin = await make_user("notadmin@example.com", is_admin=False)

    submitted = await submit_verification(
        make_submission(applicant, document_number="55566677788"),
        current_user=applicant,
    )

    # review_verification's Depends(get_current_admin) isn't invoked when
    # calling the function directly, so we assert the dependency itself
    # rejects a non-admin rather than the endpoint body.
    from app.api.deps import get_current_admin

    with pytest.raises(HTTPException) as exc_info:
        await get_current_admin(current_user=non_admin)
    assert exc_info.value.status_code == 403


async def test_verification_queue_lists_pending_only():
    applicant1 = await make_user("kyc5@example.com")
    applicant2 = await make_user("kyc6@example.com")
    admin = await make_user("admin3@example.com", is_admin=True)

    await submit_verification(
        make_submission(applicant1, document_number="10101010101"),
        current_user=applicant1,
    )
    submitted2 = await submit_verification(
        make_submission(
            applicant2,
            doc_public_id="kazihub/verifications/id-b",
            selfie_public_id="kazihub/verifications/selfie-b",
            document_type="passport",
            document_number="20202020202",
        ),
        current_user=applicant2,
    )
    await review_verification(
        submitted2.id, VerificationReview(status=VerificationStatus.APPROVED), current_user=admin
    )

    pending = await list_verification_queue(
        review_status="pending", limit=20, offset=0, current_admin=admin
    )
    assert len(pending) == 1
    assert pending[0].document_type == "nin"


async def test_upload_rejects_disallowed_content_type():
    class FakeUploadFile:
        content_type = "application/pdf"

    user = await make_user("kyc7@example.com")
    with pytest.raises(HTTPException) as exc_info:
        await upload_verification_document(file=FakeUploadFile(), current_user=user)
    assert exc_info.value.status_code == 400


async def test_cannot_submit_a_stolen_public_id_without_its_matching_token():
    """Security-audit fix: an attacker who has seen someone else's
    public_id (e.g. embedded in a signed URL an admin viewed) but doesn't
    have *their* upload_token can't claim it as their own KYC submission."""
    owner = await make_user("kyc8_owner@example.com")
    attacker = await make_user("kyc8_attacker@example.com")

    stolen_public_id = "kazihub/verifications/owners-id-photo"
    # Attacker builds a submission around their own legitimately-uploaded
    # public_id/token, then swaps in the stolen public_id afterward without
    # a matching token for it — exactly what a tampered request would do.
    submission = make_submission(attacker, doc_public_id="kazihub/verifications/attackers-own-photo")
    submission.document_image_public_id = stolen_public_id

    with pytest.raises(HTTPException) as exc_info:
        await submit_verification(submission, current_user=attacker)
    assert exc_info.value.status_code == 400


async def test_upload_token_is_rejected_when_replayed_by_a_different_user():
    """Even a genuinely valid token is scoped to the user it was issued
    for — replaying it from another account must fail."""
    owner = await make_user("kyc9_owner@example.com")
    other = await make_user("kyc9_other@example.com")

    public_id = "kazihub/verifications/some-id-photo"
    owners_token = sign_kyc_upload(str(owner.id), public_id)

    submission = make_submission(other, doc_public_id=public_id)
    submission.document_image_upload_token = owners_token

    with pytest.raises(HTTPException) as exc_info:
        await submit_verification(submission, current_user=other)
    assert exc_info.value.status_code == 400
