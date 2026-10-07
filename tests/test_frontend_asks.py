"""End-to-end checks for the frontend requirements page (asks 1-41),
through the real HTTP stack."""
import asyncio
from datetime import date, timedelta

import pyotp
import pytest

from app.core.encryption import encrypt_str
from app.models.booking import Booking, BookingStatus, EscrowStatus
from app.models.chat import Conversation
from app.models.gig import Gig
from app.models.notification import Notification
from app.models.portfolio import PortfolioItem
from app.models.review import Review
from app.models.service import Service
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from tests.http_helpers import (
    API,
    auth_headers,
    client,
    give_bank_account,
    login_pair,
    make_artisan,
    make_user,
)

pytestmark = pytest.mark.asyncio


# ---------------- Artisan directory (1-6) ----------------

async def test_listing_has_names_photos_and_hides_unfinished_profiles():
    await make_artisan("done@example.com", category="Plumbers")
    await make_artisan("empty@example.com", category="")
    async with client() as c:
        r = await c.get(f"{API}/profiles/")
    body = r.json()
    assert r.status_code == 200
    assert body["meta"]["total"] == 1
    assert body["data"][0]["first_name"] == "Bola"
    assert "profile_picture" in body["data"][0]


async def test_category_must_be_one_of_the_16():
    artisan, _ = await make_artisan("cat@example.com")
    async with client() as c:
        r = await c.put(f"{API}/profiles/me", headers=await auth_headers(artisan), json={"category": "capentary"})
        ok = await c.put(f"{API}/profiles/me", headers=await auth_headers(artisan), json={"category": "Carpenters"})
    assert r.status_code == 422
    assert ok.status_code == 200 and ok.json()["category"] == "Carpenters"


async def test_availability_filters_have_one_meaning_each():
    await make_artisan("on@example.com", is_available=True, availability_status="Available", is_available_now=True)
    await make_artisan("off@example.com", is_available=True, availability_status="Offline")
    await make_artisan("closed@example.com", is_available=False)
    await make_artisan("verified@example.com", is_verified=True, is_available=False)
    async with client() as c:
        available = (await c.get(f"{API}/profiles/?available_only=true")).json()["meta"]["total"]
        online = (await c.get(f"{API}/profiles/?online_now=true")).json()["meta"]["total"]
        verified = (await c.get(f"{API}/profiles/?verified_only=true")).json()["meta"]["total"]
    assert (available, online, verified) == (2, 1, 1)


async def test_health_reports_status_and_paystack_mode():
    async with client() as c:
        r = await c.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["paystack_mode"] in ("test", "live", "unconfigured")


# ---------------- Profile editing (7) ----------------

async def test_portfolio_item_can_be_edited_in_place():
    artisan, profile = await make_artisan("pf@example.com")
    item = PortfolioItem(artisan_profile=profile, title="Old", category="Plumbers", image_url="https://x/a.jpg")
    await item.insert()
    async with client() as c:
        r = await c.patch(
            f"{API}/profiles/me/portfolio/{item.id}", headers=await auth_headers(artisan),
            json={"title": "New", "date_completed": str(date.today())},
        )
    assert r.status_code == 200
    assert r.json()["id"] == str(item.id) and r.json()["title"] == "New"


# ---------------- Account & security (8-11, 17-20, 38, 41) ----------------

async def test_frozen_account_is_refused_everywhere_except_the_way_out():
    user = await make_user("frozen@example.com")
    headers = await auth_headers(user)
    async with client() as c:
        assert (await c.post(f"{API}/auth/freeze-me", headers=headers)).status_code == 200
        again = await c.post(f"{API}/auth/freeze-me", headers=headers)
        me = await c.get(f"{API}/auth/me", headers=headers)
        blocked = await c.put(f"{API}/auth/me", headers=headers, json={"first_name": "Bob"})
        out = await c.post(f"{API}/auth/unfreeze-me", headers=headers)
        unfrozen_again = await c.post(f"{API}/auth/unfreeze-me", headers=headers)
    assert again.status_code == 409
    assert me.json()["is_paused"] is True
    assert blocked.status_code == 423 and blocked.json()["code"] == "account_frozen"
    assert out.status_code == 200
    assert unfrozen_again.status_code == 409


async def test_frozen_artisan_page_is_404_and_cannot_be_messaged():
    client_user = await make_user("cl@example.com")
    artisan, profile = await make_artisan("fa@example.com")
    conv = Conversation(client=client_user, artisan=artisan)
    await conv.insert()
    artisan.is_paused = True
    await artisan.save()
    await profile.set({"is_paused": True})
    async with client() as c:
        page = await c.get(f"{API}/profiles/{profile.id}")
        msg = await c.post(
            f"{API}/conversations/{conv.id}/messages", headers=await auth_headers(client_user),
            json={"content": "hello"},
        )
    assert page.status_code == 404
    assert msg.status_code == 423 and msg.json()["code"] == "recipient_unavailable"


async def test_logout_signs_out_only_this_device_immediately():
    user = await make_user("lo@example.com")
    phone = await login_pair(user)
    laptop = await login_pair(user)
    async with client() as c:
        r = await c.post(f"{API}/auth/logout", json={"refresh_token": phone.refresh_token})
        phone_me = await c.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {phone.access_token}"})
        laptop_me = await c.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {laptop.access_token}"})
        refresh = await c.post(f"{API}/auth/refresh", json={"refresh_token": phone.refresh_token})
    assert r.status_code == 204
    assert phone_me.status_code == 401 and phone_me.json()["code"] == "session_signed_out"
    assert laptop_me.status_code == 200
    assert refresh.json() == {"detail": "This session was signed out.", "code": "session_signed_out"}


async def test_sessions_mark_the_current_device():
    user = await make_user("cur@example.com")
    mine = await login_pair(user)
    await login_pair(user)
    async with client() as c:
        r = await c.get(f"{API}/auth/sessions", headers={"Authorization": f"Bearer {mine.access_token}"})
    flags = [s["is_current"] for s in r.json()]
    assert sorted(flags) == [False, True]


async def test_two_factor_can_be_disabled_with_a_backup_code():
    user = await make_user("tfa@example.com")
    headers = await auth_headers(user)
    async with client() as c:
        secret = (await c.post(f"{API}/auth/2fa/setup", headers=headers)).json()["secret"]
        enabled = await c.post(
            f"{API}/auth/2fa/verify", headers=headers, json={"totp_code": pyotp.TOTP(secret).now()}
        )
        backup = enabled.json()["backup_codes"][0]
        disabled = await c.post(
            f"{API}/auth/2fa/disable", headers=headers,
            json={"current_password": "Passw0rd!", "totp_code": backup},
        )
    assert len(enabled.json()["backup_codes"]) == 10
    assert disabled.status_code == 200


async def test_login_reports_machine_readable_codes():
    user = await make_user("codes@example.com")
    user.two_factor_enabled = True
    user.two_factor_secret_encrypted = encrypt_str(pyotp.random_base32())
    await user.save()
    form = {"username": user.email, "password": "Passw0rd!"}
    async with client() as c:
        wrong_pw = await c.post(f"{API}/auth/login", data={**form, "password": "nope"})
        missing = await c.post(f"{API}/auth/login", data=form)
        bad = await c.post(f"{API}/auth/login", data={**form, "totp_code": "000000"})
    assert wrong_pw.json()["code"] == "invalid_credentials"
    assert missing.json()["code"] == "totp_required"
    assert bad.json()["code"] == "totp_invalid"


async def test_customer_privacy_and_language_validation():
    user = await make_user("priv@example.com")
    headers = await auth_headers(user)
    async with client() as c:
        ok = await c.put(f"{API}/auth/me", headers=headers, json={"phone_visibility": "hidden", "preferred_language": "yo"})
        bad_lang = await c.put(f"{API}/auth/me", headers=headers, json={"preferred_language": "klingon-xx"})
        bad_theme = await c.put(f"{API}/auth/me", headers=headers, json={"theme": "neon"})
        bad_phone = await c.put(f"{API}/auth/me", headers=headers, json={"phone_number": "+2341234"})
        old_phone = await c.put(f"{API}/auth/me", headers=headers, json={"phone_number": "08031234567"})
    assert ok.json()["phone_visibility"] == "hidden" and ok.json()["preferred_language"] == "yo"
    assert (bad_lang.status_code, bad_theme.status_code, bad_phone.status_code) == (422, 422, 422)
    assert old_phone.json()["phone_number"] == "+2348031234567"


async def test_register_requires_terms_and_password_rule():
    base = {
        "first_name": "Ada", "last_name": "Obi", "email": "new@example.com", "password": "Passw0rd!",
        "phone_number": "+2348031234567", "state": "Lagos", "role": "client",
    }
    async with client() as c:
        no_terms = await c.post(f"{API}/auth/register", json=base)
        short_pw = await c.post(f"{API}/auth/register", json={**base, "terms_version": "v1", "password": "abc"})
    assert no_terms.status_code == 422
    assert short_pw.status_code == 422


# ---------------- Payouts (21) ----------------

async def test_bank_list_comes_from_paystack(monkeypatch):
    from app.api.v1.endpoints import payments

    async def fake_list_banks():
        return [
            {"code": "058", "name": "GTBank", "slug": "gtbank", "active": True, "supports_transfer": True},
            {"code": "044", "name": "Access Bank", "slug": "access", "active": True, "supports_transfer": True},
            {"code": "50572", "name": "BANKIT MFB", "slug": "bankit-mfb-ng", "supports_transfer": True},
            {"code": "50572", "name": "BANKIT MICROFINANCE BANK LTD", "slug": "bankit-ltd", "supports_transfer": True},
            {"code": "999", "name": "No Transfers MFB", "slug": "nt", "supports_transfer": False},
        ]

    monkeypatch.setattr(payments, "list_banks", fake_list_banks)
    payments._banks_cache.update({"at": 0.0, "banks": []})
    user = await make_user("bank@example.com")
    async with client() as c:
        r = await c.get(f"{API}/payments/banks", headers=await auth_headers(user))
    assert [b["name"] for b in r.json()] == ["Access Bank", "BANKIT MICROFINANCE BANK LTD", "GTBank"]
    assert r.json()[0] == {"code": "044", "name": "Access Bank", "slug": "access"}


# ---------------- Bookings & escrow (23-30) ----------------

async def _fixed_service(profile, **kw) -> Service:
    service = Service(artisan_profile=profile, name="Fix leak", category="Plumbers",
                      pricing_type=kw.pop("pricing_type", "fixed"), price=kw.pop("price", 15000), **kw)
    await service.insert()
    return service


async def test_fixed_booking_is_priced_from_the_service():
    client_user = await make_user("fx@example.com")
    artisan, profile = await make_artisan("fxa@example.com")
    service = await _fixed_service(profile)
    quote_only = await _fixed_service(profile, pricing_type="quote_required", price=0)
    tomorrow = str(date.today() + timedelta(days=1))
    body = {
        "artisan_id": str(artisan.id), "service_id": str(service.id), "amount": 1,
        "description": "Kitchen sink", "address": "1 Road",
        "scheduled_date": tomorrow, "scheduled_window": "09:00-11:00",
    }
    async with client() as c:
        r = await c.post(f"{API}/bookings/fixed", headers=await auth_headers(client_user, True), json=body)
        refused = await c.post(
            f"{API}/bookings/fixed", headers=await auth_headers(client_user, True),
            json={**body, "service_id": str(quote_only.id)},
        )
        bad_window = await c.post(
            f"{API}/bookings/fixed", headers=await auth_headers(client_user, True),
            json={**body, "scheduled_window": "11:00-09:00"},
        )
    b = r.json()
    assert r.status_code == 201
    assert (b["amount"], b["title"]) == (15000, "Fix leak")
    assert (b["scheduled_date"], b["scheduled_window"]) == (tomorrow, "09:00-11:00")
    assert b["artisan_name"] == "Bola Ade" and b["client_name"] == "Ada Obi"
    assert b["artisan_profile_id"] == str(profile.id) and b["artisan_category"] == "Plumbers"
    assert b["created_at"].endswith("Z")
    assert refused.status_code == 400
    assert bad_window.status_code == 422
    notes = await Notification.find({"type": "booking_requested"}).to_list()
    assert len(notes) == 1 and notes[0].booking is not None


async def test_unfunded_booking_is_never_paid_out(monkeypatch):
    calls = []

    async def fake_transfer(**kw):
        calls.append(kw)
        return {}

    monkeypatch.setattr("app.services.payouts.initiate_transfer", fake_transfer)
    client_user = await make_user("uf@example.com")
    artisan, _ = await make_artisan("ufa@example.com")
    await give_bank_account(artisan)
    booking = Booking(client=client_user, artisan=artisan, title="Job", amount=5000, status=BookingStatus.ACCEPTED)
    await booking.insert()
    async with client() as c:
        r = await c.post(
            f"{API}/bookings/{booking.id}/confirm-completion", headers=await auth_headers(client_user, True)
        )
    assert r.status_code == 400 and "status 'accepted'" in r.json()["detail"]
    assert calls == []
    assert await Transaction.find_one({}) is None


async def test_paid_job_start_completion_proof_and_phone_sharing(monkeypatch):
    client_user = await make_user("pj@example.com", phone_number="+2348011111111")
    artisan, profile = await make_artisan("pja@example.com")
    booking = Booking(
        client=client_user, artisan=artisan, title="Job", amount=5000, escrow_amount=5000,
        status=BookingStatus.ESCROW_FUNDED, escrow_status=EscrowStatus.HELD_IN_ESCROW,
    )
    await booking.insert()
    unpaid = Booking(client=client_user, artisan=artisan, title="Later", amount=1, status=BookingStatus.PENDING)
    await unpaid.insert()
    async with client() as c:
        started = await c.post(f"{API}/bookings/{booking.id}/start", headers=await auth_headers(artisan))
        done = await c.post(
            f"{API}/bookings/{booking.id}/submit-completion", headers=await auth_headers(artisan),
            json={"completion_description": "Fixed", "completion_photos": ["https://x/p.jpg"]},
        )
        unpaid_view = await c.get(f"{API}/bookings/{unpaid.id}", headers=await auth_headers(artisan))
    assert started.json()["status"] == "in_progress"
    assert done.json()["completion_photos"] == ["https://x/p.jpg"]
    assert done.json()["client_phone"] == "+2348011111111"
    assert unpaid_view.json()["client_phone"] is None
    types = {n.type for n in await Notification.find({}).to_list()}
    assert {"job_started", "completion_submitted"} <= types


async def test_fund_escrow_confirms_with_paystack_when_webhook_is_late(monkeypatch):
    from app.api.v1.endpoints import bookings

    async def fake_verify(reference):
        return {"status": "success", "amount": 500000, "reference": reference}

    monkeypatch.setattr(bookings, "verify_transaction", fake_verify)
    client_user = await make_user("fe@example.com")
    artisan, _ = await make_artisan("fea@example.com")
    booking = Booking(client=client_user, artisan=artisan, title="Job", amount=5000, escrow_amount=5000,
                      status=BookingStatus.ACCEPTED)
    await booking.insert()
    await Transaction(booking=booking, transaction_reference="KZ-ESCROW-1", amount=5000,
                      type=TransactionType.ESCROW_DEPOSIT, status=TransactionStatus.PENDING).insert()
    async with client() as c:
        r = await c.post(f"{API}/bookings/{booking.id}/fund-escrow", headers=await auth_headers(client_user, True))
    assert r.status_code == 200 and r.json()["status"] == "escrow_funded"


async def test_idempotency_key_is_a_declared_header():
    from app.main import app

    params = app.openapi()["paths"]["/api/v1/bookings/fixed"]["post"]["parameters"]
    assert any(p["name"] == "Idempotency-Key" and p["required"] for p in params)


# ---------------- Notifications & chat (31, 32, 35, 36, 39, 40) ----------------

async def test_chat_times_order_names_and_delete_for_me():
    client_user = await make_user("ch@example.com")
    artisan, _ = await make_artisan("cha@example.com")
    conv = Conversation(client=client_user, artisan=artisan)
    await conv.insert()
    async with client() as c:
        for text in ("one", "two"):
            await c.post(f"{API}/conversations/{conv.id}/messages", headers=await auth_headers(client_user),
                         json={"content": text})
            await asyncio.sleep(0.01)
        msgs = (await c.get(f"{API}/conversations/{conv.id}/messages", headers=await auth_headers(artisan))).json()
        inbox = (await c.get(f"{API}/conversations", headers=await auth_headers(artisan))).json()
        await c.delete(f"{API}/conversations/{conv.id}", headers=await auth_headers(artisan))
        after_delete = (await c.get(f"{API}/conversations", headers=await auth_headers(artisan))).json()
        await c.post(f"{API}/conversations/{conv.id}/messages", headers=await auth_headers(client_user),
                     json={"content": "three"})
        back = (await c.get(f"{API}/conversations", headers=await auth_headers(artisan))).json()
        history = (await c.get(f"{API}/conversations/{conv.id}/messages", headers=await auth_headers(artisan))).json()
    assert [m["content"] for m in msgs] == ["one", "two"]
    assert msgs[0]["created_at"] != msgs[1]["created_at"] and msgs[0]["created_at"].endswith("Z")
    assert inbox[0]["client_name"] == "Ada Obi" and inbox[0]["unread_count"] == 2
    assert after_delete == []
    assert len(back) == 1 and [m["content"] for m in history] == ["three"]
    assert await Notification.find({"type": "new_message"}).count() == 3


async def test_voice_note_upload_accepts_iphone_audio(monkeypatch):
    from app.api.v1.endpoints import chat

    async def fake_audio_upload(file, folder):
        return {"url": "https://res.cloudinary.com/x/video/upload/v1/a.m4a", "original_url": "https://x/a.mp4"}

    monkeypatch.setattr(chat, "upload_audio_to_cloudinary", fake_audio_upload)
    user = await make_user("vn@example.com")
    async with client() as c:
        r = await c.post(
            f"{API}/chat/upload-media", headers=await auth_headers(user),
            files={"file": ("note.m4a", b"\x00" * 100, "audio/mp4")},
        )
    assert r.status_code == 200
    assert r.json()["media_type"] == "audio" and r.json()["url"].endswith(".m4a")


async def test_ws_ticket_documents_the_socket():
    user = await make_user("ws@example.com")
    async with client() as c:
        r = await c.post(f"{API}/chat/ws-ticket", headers=await auth_headers(user))
    assert r.json()["expires_in"] == 30 and "{conversation_id}" in r.json()["websocket_path"]


async def test_featured_reviews_only_show_consented_ones():
    client_user = await make_user("rv@example.com")
    artisan, _ = await make_artisan("rva@example.com")
    booking = Booking(client=client_user, artisan=artisan, title="Job", amount=1, status=BookingStatus.PAID_OUT)
    await booking.insert()
    await Review(booking=booking, client=client_user, artisan=artisan, rating=5, comment="Great",
                 client_name="Ada Obi", share_publicly=True).insert()
    await Review(booking=booking, client=client_user, artisan=artisan, rating=5, comment="Private",
                 client_name="Ada Obi").insert()
    async with client() as c:
        r = await c.get(f"{API}/reviews/featured")
    assert [x["comment"] for x in r.json()] == ["Great"]
    assert r.json()[0]["client_first_name"] == "Ada" and r.json()[0]["category"] == "Plumbers"


async def test_notification_preferences_round_trip():
    user = await make_user("np@example.com")
    headers = await auth_headers(user)
    async with client() as c:
        r = await c.put(f"{API}/notifications/preferences", headers=headers, json={"email_summaries": False})
        types = await c.get(f"{API}/notifications/types", headers=headers)
    assert r.json() == {"push_enabled": True, "email_summaries": False}
    assert "quote_sent" in types.json()


# ---------------- Errors, filtering, validation (33, 34, 37) ----------------

async def test_gigs_filter_by_artisan():
    _, p1 = await make_artisan("g1@example.com")
    _, p2 = await make_artisan("g2@example.com")
    for p in (p1, p2):
        await Gig(artisan_profile=p, title="G", description="d", category="Plumbers", price=10,
                  delivery_time_days=1).insert()
    async with client() as c:
        r = await c.get(f"{API}/gigs/?artisan_profile_id={p1.id}")
    assert len(r.json()) == 1 and r.json()[0]["artisan_profile_id"] == str(p1.id)


async def test_profile_field_rules():
    artisan, _ = await make_artisan("rules@example.com")
    headers = await auth_headers(artisan)
    cases = [
        {"response_time": "ssmsm"},
        {"neighborhood": "Lagos"},
        {"skills": ["a"] * 13},
        {"years_of_experience": 61},
        {"pricing_type": "starting", "base_price": 0},
        {"tagline": "x" * 61},
    ]
    async with client() as c:
        codes = [(await c.put(f"{API}/profiles/me", headers=headers, json=body)).status_code for body in cases]
        good = await c.put(f"{API}/profiles/me", headers=headers, json={
            "response_time": "Within 1 hour", "neighborhood": "Yaba", "skills": ["Pipes"],
            "pricing_type": "quote_required", "base_price": 0,
        })
    assert codes == [422] * len(cases)
    assert good.status_code == 200


# ---------------- Docs (15, 22) & support/export (13, 14) ----------------

async def test_upload_endpoints_declare_their_responses():
    from app.main import app

    paths = app.openapi()["paths"]
    for path in ("/api/v1/profiles/me/portfolio/upload", "/api/v1/gigs/upload", "/api/v1/chat/upload-media",
                 "/api/v1/verification/upload", "/api/v1/bookings/upload"):
        schema = paths[path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert "$ref" in schema, path


async def test_favorites_accept_profile_id_too():
    user = await make_user("fav@example.com")
    _, profile = await make_artisan("fava@example.com")
    async with client() as c:
        r = await c.post(f"{API}/favorites/{profile.id}", headers=await auth_headers(user))
    assert r.status_code == 201


async def test_support_ticket_and_data_export():
    user = await make_user("sup@example.com")
    headers = await auth_headers(user)
    async with client() as c:
        ticket = await c.post(f"{API}/support/tickets", headers=headers, json={"subject": "Help", "message": "Hi"})
        export = await c.get(f"{API}/auth/me/export", headers=headers)
    assert ticket.status_code == 201 and ticket.json()["ticket_number"].startswith("SUP-")
    data = export.json()
    assert data["account"]["email"] == "sup@example.com"
    assert data["support_requests"][0]["ticket_number"] == ticket.json()["ticket_number"]
    assert "hashed_password" not in export.text


async def test_proxy_ip_uses_the_entry_the_proxy_appended(monkeypatch):
    from starlette.requests import Request

    from app.core import rate_limit

    monkeypatch.setattr(rate_limit.settings, "TRUST_PROXY_HEADERS", True)
    request = Request({
        "type": "http", "headers": [(b"x-forwarded-for", b"6.6.6.6, 41.58.1.2")], "client": ("10.0.0.1", 1),
    })
    assert rate_limit.get_client_ip(request) == "41.58.1.2"  # not the forgeable left-most entry


# ---------------- Round 2 (7 October 2026): asks 11, 43, 44, 46 ----------------

async def test_phone_visibility_is_an_enum_in_the_schema():
    from app.main import app

    prop = app.openapi()["components"]["schemas"]["UserResponse"]["properties"]["phone_visibility"]
    assert prop["enum"] == ["after_escrow", "verified_only", "hidden"]
    assert "after_escrow" in prop["description"]


async def test_saved_artisans_carry_names_and_profile_id():
    user = await make_user("fav2@example.com")
    artisan, profile = await make_artisan("fav2a@example.com")
    async with client() as c:
        await c.post(f"{API}/favorites/{artisan.id}", headers=await auth_headers(user))
        r = await c.get(f"{API}/favorites/", headers=await auth_headers(user))
    item = r.json()[0]
    assert (item["first_name"], item["last_name"], item["artisan_profile_id"]) == ("Bola", "Ade", str(profile.id))


async def test_backup_code_works_lowercase_and_without_hyphen():
    user = await make_user("bc@example.com")
    headers = await auth_headers(user)
    async with client() as c:
        secret = (await c.post(f"{API}/auth/2fa/setup", headers=headers)).json()["secret"]
        codes = (await c.post(f"{API}/auth/2fa/verify", headers=headers,
                              json={"totp_code": pyotp.TOTP(secret).now()})).json()["backup_codes"]
        assert all(len(code) == 9 and code[4] == "-" for code in codes)
        r = await c.post(f"{API}/auth/login", data={
            "username": user.email, "password": "Passw0rd!", "totp_code": codes[0].replace("-", "").lower(),
        })
        reused = await c.post(f"{API}/auth/login", data={
            "username": user.email, "password": "Passw0rd!", "totp_code": codes[0],
        })
    assert r.status_code == 200
    assert reused.json()["code"] == "totp_invalid"


async def test_push_subscription_and_delivery(monkeypatch):
    from app.core.config import settings
    from app.models.push_subscription import PushSubscription
    from app.services import push

    sent = []
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "BPUBLIC")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "private")
    monkeypatch.setattr(push, "webpush", lambda **kw: sent.append(kw))

    client_user = await make_user("push@example.com")
    artisan, _ = await make_artisan("pusha@example.com")
    conv = Conversation(client=client_user, artisan=artisan)
    await conv.insert()
    sub = {"endpoint": "https://push.example.com/abc", "keys": {"p256dh": "key", "auth": "secret"}}
    async with client() as c:
        key = await c.get(f"{API}/notifications/push/public-key")
        r = await c.post(f"{API}/notifications/push/subscriptions", headers=await auth_headers(artisan), json=sub)
        await c.post(f"{API}/conversations/{conv.id}/messages", headers=await auth_headers(client_user),
                     json={"content": "hello"})
        await asyncio.gather(*push._pending)
        await c.request("DELETE", f"{API}/notifications/push/subscriptions",
                        headers=await auth_headers(artisan), json={"endpoint": sub["endpoint"]})
    assert key.json() == {"enabled": True, "public_key": "BPUBLIC"}
    assert r.status_code == 201
    assert len(sent) == 1 and sent[0]["subscription_info"]["endpoint"] == sub["endpoint"]
    assert '"type": "new_message"' in sent[0]["data"]
    assert await PushSubscription.find({}).count() == 0


async def test_deleted_artisan_drops_out_of_search():
    artisan, profile = await make_artisan("gone@example.com")
    async with client() as c:
        await c.delete(f"{API}/auth/me", headers=await auth_headers(artisan))
        listing = await c.get(f"{API}/profiles/")
        page = await c.get(f"{API}/profiles/{profile.id}")
    assert listing.json()["meta"]["total"] == 0
    assert page.status_code == 404
