"""One-off clean-up of live data the frontend found (requirements asks 3,
4, 20, 35 and 37). Dry run by default; pass --apply to write.

    python -m scripts.migrate_frontend_asks            # show what would change
    python -m scripts.migrate_frontend_asks --apply    # change it

What it does:
  - categories (profiles, services, portfolio): map close/legacy spellings
    to the 16 official names, e.g. "capentary" -> "Carpenters". Anything it
    can't place is listed for a human to fix.
  - users.preferred_language: full labels -> codes ("English (Nigeria)" -> "en");
    users.theme: anything not light/dark/system -> "system".
  - phone numbers: "0803..." -> "+234803...".
  - profiles.response_time not in the fixed list -> null;
    profiles.neighborhood equal to the state -> null;
    profiles.is_available_now re-derived from availability_status.
  - created_at frozen at server start (the import-time default bug): reset
    from each document's ObjectId timestamp (second precision) wherever
    created_at is more than 5 seconds earlier than the document's own
    creation. Skips users/pending_users, whose created_at is set explicitly.
"""
import argparse
import asyncio
import difflib
import re
from datetime import timedelta, timezone

import certifi
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.core.constants import CATEGORIES, LANGUAGE_LABEL_PREFIXES, LANGUAGES, RESPONSE_TIMES, THEMES

LEGACY_CATEGORY_NAMES = {
    "plumbing": "Plumbers", "plumber": "Plumbers",
    "electrical": "Electricians", "electrician": "Electricians",
    "carpentry": "Carpenters", "carpenter": "Carpenters", "capentary": "Carpenters",
    "ac technician": "AC Technicians", "appliance repair": "Appliance Repair Specialists",
    "mechanic": "Mechanics", "solar": "Solar Installers", "cctv": "CCTV Installers",
    "painting": "Painters", "painter": "Painters", "welding": "Welders", "welder": "Welders",
    "cleaning": "Cleaners", "cleaner": "Cleaners", "tutoring": "Tutors", "tutor": "Tutors",
    "tailoring": "Tailors", "tailor": "Tailors", "hair": "Hair Stylists", "hair stylist": "Hair Stylists",
    "photography": "Photographers", "photographer": "Photographers",
    "events": "Event Professionals", "event professional": "Event Professionals",
}
FROZEN_TIMESTAMP_COLLECTIONS = (
    "messages", "user_sessions", "bookings", "notifications", "booking_status_history",
    "disputes", "reviews", "audit_logs", "saved_professionals", "bank_accounts",
    "escrow_transactions", "processed_webhook_events", "idempotency_keys", "user_roles",
    "profiles", "artisan_services", "artisan_portfolios", "gigs", "verifications", "conversations",
)


def canonical_category(value):
    if value in CATEGORIES or not value:
        return value
    key = value.strip().lower()
    if key in LEGACY_CATEGORY_NAMES:
        return LEGACY_CATEGORY_NAMES[key]
    for c in CATEGORIES:
        if c.lower() == key:
            return c
    match = difflib.get_close_matches(key, [c.lower() for c in CATEGORIES] + list(LEGACY_CATEGORY_NAMES), n=1, cutoff=0.75)
    if match:
        m = match[0]
        return LEGACY_CATEGORY_NAMES.get(m) or next(c for c in CATEGORIES if c.lower() == m)
    return None


def language_code(value):
    if value in LANGUAGES:
        return value
    lowered = (value or "").strip().lower()
    for prefix, code in LANGUAGE_LABEL_PREFIXES.items():
        if lowered.startswith(prefix):
            return code
    return "en"


def normalized_phone(value):
    digits = re.sub(r"[\s\-()]", "", value or "")
    if digits.startswith("0") and len(digits) == 11:
        return "+234" + digits[1:]
    if digits.startswith("234") and len(digits) == 13:
        return "+" + digits
    return value


async def main(apply: bool) -> None:
    client = AsyncIOMotorClient(settings.MONGODB_URL, tlsCAFile=certifi.where(), tz_aware=True)
    db = client[settings.DATABASE_NAME]
    changes = 0

    async def update(coll, doc_id, fields, why):
        nonlocal changes
        changes += 1
        print(f"  {coll} {doc_id}: {why} -> {fields}")
        if apply:
            await db[coll].update_one({"_id": doc_id}, {"$set": fields})

    print("Categories")
    for coll in ("profiles", "artisan_services", "artisan_portfolios"):
        async for doc in db[coll].find({"category": {"$nin": list(CATEGORIES) + ["", None]}}):
            fixed = canonical_category(doc.get("category"))
            if fixed:
                await update(coll, doc["_id"], {"category": fixed}, f"category {doc.get('category')!r}")
            else:
                print(f"  !! {coll} {doc['_id']}: can't place category {doc.get('category')!r}; fix by hand")

    print("Users: language, theme, phone")
    async for user in db.users.find({}):
        fields = {}
        if user.get("preferred_language") not in LANGUAGES:
            fields["preferred_language"] = language_code(user.get("preferred_language"))
        if user.get("theme") not in THEMES:
            fields["theme"] = (user.get("theme") or "").lower() if (user.get("theme") or "").lower() in THEMES else "system"
        phone = normalized_phone(user.get("phone_number"))
        if phone != user.get("phone_number"):
            fields["phone_number"] = phone
        if fields:
            await update("users", user["_id"], fields, "normalise")

    print("Profiles: response time, neighbourhood, availability")
    async for p in db.profiles.find({}):
        fields = {}
        if p.get("response_time") is not None and p.get("response_time") not in RESPONSE_TIMES:
            fields["response_time"] = None
        hood, state = (p.get("neighborhood") or "").strip().lower(), (p.get("state") or "").strip().lower()
        if hood and hood == state:
            fields["neighborhood"] = None
        online = p.get("availability_status") == "Available"
        if p.get("is_available_now") != online:
            fields["is_available_now"] = online
        if fields:
            await update("profiles", p["_id"], fields, "clean")

    print("Timestamps frozen at server start")
    for coll in FROZEN_TIMESTAMP_COLLECTIONS:
        fixed = 0
        async for doc in db[coll].find({"created_at": {"$exists": True}}, {"created_at": 1}):
            created = doc["created_at"]
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            actual = doc["_id"].generation_time
            if created < actual - timedelta(seconds=5):
                fixed += 1
                changes += 1
                if apply:
                    await db[coll].update_one({"_id": doc["_id"]}, {"$set": {"created_at": actual}})
        if fixed:
            print(f"  {coll}: {fixed} created_at values reset from their ObjectId")

    client.close()
    print(f"\n{changes} change(s) {'applied' if apply else 'found (dry run; pass --apply to write)'}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    asyncio.run(main(parser.parse_args().apply))
