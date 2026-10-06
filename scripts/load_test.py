"""Concurrent load test: N simulated users (half customers, half artisans)
log in at the same moment and loop through every endpoint that is safe to
hit on a live deployment, all at once.

    python -m scripts.load_test seed     --clients 100 --artisans 2
    python -m scripts.load_test run      --base-url https://kazihub-52ph.onrender.com --duration 120
    python -m scripts.load_test cleanup

`seed` and `cleanup` write to the database in .env (MONGODB_URL /
DATABASE_NAME). Test accounts are `loadtest+<n>@example.com`, and
`cleanup` deletes them and everything they created.

Skipped on purpose (they'd send real email, move real money, or store real
files): register / verify / resend-otp / forgot & reset password / change
email, every upload, Paystack checkout / fund-escrow / bank verification,
2FA setup, freeze, admin and webhook routes. Everything after "paid into
escrow" is unreachable without a real payment.

The API's per-IP limit (GLOBAL_RATE_LIMIT_PER_MINUTE, default 100) will
otherwise throttle a test sent from one machine: raise it on the server for
the run, and put it back afterwards.
"""
import argparse
import asyncio
import json
import random
import statistics
import time
import uuid
from collections import defaultdict
from datetime import date, timedelta

import certifi
import httpx
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.core.security import get_password_hash

EMAIL_PATTERN = r"^loadtest\+\d+@example\.com$"
PASSWORD = "LoadTest!2026"
CREDENTIALS_FILE = "loadtest_accounts.json"


# ---------------------------------------------------------------- database

async def _db():
    from app.main import (  # every registered model, same list the app uses
        AuditLog, BankAccount, Booking, BookingStatusHistory, Conversation, Dispute, Gig,
        IdempotencyRecord, Message, Notification, PendingUser, PortfolioItem, Profile, Review,
        SavedProfessional, Service, SupportTicket, Transaction, User, UserRole, UserSession,
        Verification, ProcessedWebhookEvent,
    )
    client = AsyncIOMotorClient(settings.MONGODB_URL, tlsCAFile=certifi.where(), tz_aware=True)
    await init_beanie(
        database=client[settings.DATABASE_NAME],
        document_models=[
            User, UserRole, Profile, Service, PortfolioItem, Gig, Verification, PendingUser,
            Conversation, Message, Booking, BookingStatusHistory, Dispute, Review, Notification,
            AuditLog, SavedProfessional, BankAccount, Transaction, ProcessedWebhookEvent,
            UserSession, IdempotencyRecord, SupportTicket,
        ],
    )
    return client


async def seed(n_clients: int, n_artisans: int) -> None:
    from app.models.gig import Gig
    from app.models.profile import Profile
    from app.models.service import Service
    from app.models.user import User
    from app.models.user_role import UserRole

    client = await _db()
    if await User.find({"email": {"$regex": EMAIL_PATTERN}}).count():
        raise SystemExit("Load-test accounts already exist; run `cleanup` first.")

    hashed = get_password_hash(PASSWORD)
    accounts = []
    n = n_clients + n_artisans
    for i in range(n):
        is_artisan = i < n_artisans
        user = User(
            first_name="Load", last_name=f"Tester{i}", email=f"loadtest+{i}@example.com",
            phone_number=f"+23480{10000000 + i:08d}", state="Lagos",
            role="artisan" if is_artisan else "client",
            roles=["client", "artisan"] if is_artisan else ["client"],
            hashed_password=hashed, is_email_verified=True, terms_version="loadtest",
        )
        await user.insert()
        for r in user.roles:
            await UserRole(user=user, role=r).insert()
        entry = {"email": user.email, "user_id": str(user.id), "role": user.role}
        if is_artisan:
            profile = Profile(
                user=user, category="Plumbers", state="Lagos", neighborhood="Yaba",
                tagline="Load test artisan", pricing_type="fixed", base_price=10000,
                is_available=True, availability_status="Available", is_available_now=True,
            )
            await profile.insert()
            service = Service(artisan_profile=profile, name="Leak repair", category="Plumbers",
                              pricing_type="fixed", price=10000, duration_estimate="1-2 hrs")
            await service.insert()
            await Gig(artisan_profile=profile, title="Tap install", description="Load test gig",
                      category="Plumbers", price=8000, delivery_time_days=1).insert()
            entry.update(profile_id=str(profile.id), service_id=str(service.id))
        accounts.append(entry)

    # Spread the customers across the artisans, round robin.
    artisans = [a for a in accounts if a["role"] == "artisan"]
    for i, a in enumerate(a for a in accounts if a["role"] == "client"):
        a["partner"] = artisans[i % len(artisans)]
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(accounts, f, indent=2)
    client.close()
    print(f"Seeded {n} accounts ({n - len(artisans)} customers, {len(artisans)} artisans) into {settings.DATABASE_NAME}; wrote {CREDENTIALS_FILE}.")


async def cleanup() -> None:
    from beanie import PydanticObjectId
    from app.main import (
        Booking, BookingStatusHistory, Conversation, Dispute, Gig, IdempotencyRecord, Message,
        Notification, PortfolioItem, Profile, Review, SavedProfessional, Service, SupportTicket,
        Transaction, User, UserRole, UserSession,
    )

    client = await _db()
    users = await User.find({"email": {"$regex": EMAIL_PATTERN}}).to_list()
    ids = [u.id for u in users]
    if not ids:
        print("Nothing to clean up.")
        client.close()
        return
    profiles = await Profile.find({"user.$id": {"$in": ids}}).to_list()
    pids = [p.id for p in profiles]
    bookings = await Booking.find({"$or": [{"client.$id": {"$in": ids}}, {"artisan.$id": {"$in": ids}}]}).to_list()
    bids = [b.id for b in bookings]
    convs = await Conversation.find({"$or": [{"client.$id": {"$in": ids}}, {"artisan.$id": {"$in": ids}}]}).to_list()
    cids = [str(c.id) for c in convs]

    deletions = [
        ("messages", Message.find({"conversation_id": {"$in": cids}})),
        ("conversations", Conversation.find({"_id": {"$in": [c.id for c in convs]}})),
        ("booking_status_history", BookingStatusHistory.find({"booking.$id": {"$in": bids}})),
        ("transactions", Transaction.find({"booking.$id": {"$in": bids}})),
        ("disputes", Dispute.find({"booking.$id": {"$in": bids}})),
        ("reviews", Review.find({"booking.$id": {"$in": bids}})),
        ("bookings", Booking.find({"_id": {"$in": bids}})),
        ("gigs", Gig.find({"artisan_profile.$id": {"$in": pids}})),
        ("services", Service.find({"artisan_profile.$id": {"$in": pids}})),
        ("portfolio", PortfolioItem.find({"artisan_profile.$id": {"$in": pids}})),
        ("profiles", Profile.find({"_id": {"$in": pids}})),
        ("notifications", Notification.find({"user.$id": {"$in": ids}})),
        ("sessions", UserSession.find({"user.$id": {"$in": ids}})),
        ("favorites", SavedProfessional.find({"$or": [{"user.$id": {"$in": ids}}, {"artisan.$id": {"$in": ids}}]})),
        ("support_tickets", SupportTicket.find({"user.$id": {"$in": ids}})),
        ("idempotency", IdempotencyRecord.find({"user.$id": {"$in": ids}})),
        ("user_roles", UserRole.find({"user.$id": {"$in": ids}})),
        ("users", User.find({"_id": {"$in": ids}})),
    ]
    for name, query in deletions:
        result = await query.delete()
        print(f"  {name}: {getattr(result, 'deleted_count', '?')} deleted")
    client.close()
    print("Clean-up done.")


# ---------------------------------------------------------------- traffic

class Stats:
    def __init__(self):
        self.latency = defaultdict(list)
        self.status = defaultdict(lambda: defaultdict(int))
        self.samples = defaultdict(dict)
        self.login_seconds = []

    def record(self, name, status, ms, body=None):
        self.latency[name].append(ms)
        self.status[name][status] += 1
        if status >= 400 and status not in self.samples[name]:
            self.samples[name][status] = (body or "")[:200]


class VirtualUser:
    def __init__(self, http: httpx.AsyncClient, account: dict, stats: Stats):
        self.http, self.account, self.stats = http, account, stats
        self.headers = {}
        self.refresh_token = None

    async def call(self, name, method, path, expect=(200, 201, 204), idempotent=False, **kw):
        headers = dict(self.headers)
        if idempotent:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        t = time.perf_counter()
        try:
            r = await self.http.request(method, path, headers=headers, **kw)
            status, text = r.status_code, r.text
        except httpx.HTTPError as e:
            status, text, r = 599, f"{type(e).__name__}: {e}", None
        self.stats.record(name, status, (time.perf_counter() - t) * 1000, text if status not in expect else None)
        if r is not None and status in expect and r.content and r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return None

    async def login(self):
        """Up to 3 attempts, like a person pressing Sign in again. Records
        the total time until this user is actually in."""
        t = time.perf_counter()
        data = None
        for _ in range(3):
            data = await self.call("POST /auth/login", "POST", "/api/v1/auth/login", timeout=120,
                                   data={"username": self.account["email"], "password": PASSWORD})
            if data:
                break
        if not data:
            raise RuntimeError(f"login failed after 3 tries for {self.account['email']}")
        self.stats.login_seconds.append(time.perf_counter() - t)
        self.headers = {"Authorization": f"Bearer {data['access_token']}"}
        self.refresh_token = data["refresh_token"]

    async def common(self):
        v1 = "/api/v1"
        await self.call("GET /auth/me", "GET", f"{v1}/auth/me")
        await self.call("GET /auth/sessions", "GET", f"{v1}/auth/sessions")
        await self.call("PUT /auth/me", "PUT", f"{v1}/auth/me", json={"theme": random.choice(["light", "dark"])})
        await self.call("GET /notifications/", "GET", f"{v1}/notifications/")
        await self.call("PATCH /notifications/read-all", "PATCH", f"{v1}/notifications/read-all")
        await self.call("GET /notifications/preferences", "GET", f"{v1}/notifications/preferences")
        await self.call("PUT /notifications/preferences", "PUT", f"{v1}/notifications/preferences",
                        json={"email_summaries": False})
        await self.call("GET /profiles/", "GET", f"{v1}/profiles/?limit=20")
        await self.call("GET /profiles/?category", "GET", f"{v1}/profiles/?category=Plumbers&available_only=true")
        await self.call("GET /gigs/", "GET", f"{v1}/gigs/?limit=20")
        await self.call("GET /reviews/featured", "GET", f"{v1}/reviews/featured")
        await self.call("GET /conversations", "GET", f"{v1}/conversations")
        await self.call("GET /bookings/me", "GET", f"{v1}/bookings/me")
        await self.call("POST /chat/ws-ticket", "POST", f"{v1}/chat/ws-ticket")
        await self.call("GET /health", "GET", "/health")

    async def customer_round(self, round_no: int):
        v1, partner = "/api/v1", self.account["partner"]
        await self.call("GET /profiles/{id}", "GET", f"{v1}/profiles/{partner['profile_id']}")
        await self.call("GET /reviews/pro/{id}", "GET", f"{v1}/reviews/pro/{partner['user_id']}")
        await self.call("GET /gigs/?artisan", "GET", f"{v1}/gigs/?artisan_profile_id={partner['profile_id']}")
        conv = await self.call("POST /conversations", "POST", f"{v1}/conversations",
                               json={"artisan_id": partner["user_id"]})
        if conv:
            await self.call("POST /conversations/{id}/messages", "POST",
                            f"{v1}/conversations/{conv['id']}/messages",
                            json={"content": f"Load test message {round_no}"})
            await self.call("GET /conversations/{id}/messages", "GET", f"{v1}/conversations/{conv['id']}/messages")
            await self.call("PATCH /conversations/{id}/read", "PATCH", f"{v1}/conversations/{conv['id']}/read")
        await self.call("POST /favorites/{id}", "POST", f"{v1}/favorites/{partner['user_id']}")
        await self.call("GET /favorites/", "GET", f"{v1}/favorites/")
        await self.call("DELETE /favorites/{id}", "DELETE", f"{v1}/favorites/{partner['user_id']}")

        when = str(date.today() + timedelta(days=3))
        await self.call("POST /bookings/quote-request", "POST", f"{v1}/bookings/quote-request", idempotent=True,
                        json={"artisan_id": partner["user_id"], "service_title": "Bathroom leak",
                              "description": "Load test", "address": "1 Test Road",
                              "scheduled_date": when, "scheduled_window": "09:00-11:00"})
        fixed = await self.call("POST /bookings/fixed", "POST", f"{v1}/bookings/fixed", idempotent=True,
                                json={"artisan_id": partner["user_id"], "service_id": partner["service_id"],
                                      "description": "Load test", "address": "1 Test Road"})
        if fixed:
            await self.call("GET /bookings/{id}", "GET", f"{v1}/bookings/{fixed['id']}")
            if round_no % 2:
                await self.call("POST /bookings/{id}/cancel", "POST", f"{v1}/bookings/{fixed['id']}/cancel")
        quotes = await self.call("GET /bookings/me?status", "GET", f"{v1}/bookings/me?status=quote_sent") or []
        for b in quotes[:2]:
            await self.call("POST /bookings/{id}/quote/accept", "POST", f"{v1}/bookings/{b['id']}/quote/accept",
                            expect=(200, 400, 409))
        if round_no == 0:
            await self.call("POST /support/tickets", "POST", f"{v1}/support/tickets",
                            json={"subject": "Load test", "message": "Ignore: automated load test"})
            await self.call("GET /auth/me/export", "GET", f"{v1}/auth/me/export")

    async def artisan_round(self, round_no: int):
        v1 = "/api/v1"
        await self.call("GET /profiles/me", "GET", f"{v1}/profiles/me")
        await self.call("PUT /profiles/me", "PUT", f"{v1}/profiles/me",
                        json={"tagline": f"Load test round {round_no}"})
        await self.call("GET /profiles/me/services/", "GET", f"{v1}/profiles/me/services/")
        await self.call("GET /profiles/me/portfolio/", "GET", f"{v1}/profiles/me/portfolio/")
        await self.call("GET /gigs/my-gigs", "GET", f"{v1}/gigs/my-gigs")
        requested = await self.call("GET /bookings/me?status", "GET", f"{v1}/bookings/me?status=quote_requested") or []
        for b in requested[:10]:
            await self.call("POST /bookings/{id}/quote", "POST", f"{v1}/bookings/{b['id']}/quote",
                            json={"amount": 15000, "breakdown": "Labour"}, expect=(200, 400, 409))
        pending = await self.call("GET /bookings/me?status", "GET", f"{v1}/bookings/me?status=pending") or []
        for i, b in enumerate(pending[:10]):
            action = "accept" if i % 2 == 0 else "decline"
            await self.call(f"POST /bookings/{{id}}/{action}", "POST", f"{v1}/bookings/{b['id']}/{action}",
                            expect=(200, 400, 409))
        convs = await self.call("GET /conversations", "GET", f"{v1}/conversations") or []
        for conv in convs[:5]:
            await self.call("GET /conversations/{id}/messages", "GET", f"{v1}/conversations/{conv['id']}/messages")
            await self.call("POST /conversations/{id}/messages", "POST",
                            f"{v1}/conversations/{conv['id']}/messages", json={"content": "On my way"})
            await self.call("PATCH /conversations/{id}/read", "PATCH", f"{v1}/conversations/{conv['id']}/read")
        if round_no == 0:
            await self.call("GET /payments/banks", "GET", f"{v1}/payments/banks")
            await self.call("GET /auth/me/export", "GET", f"{v1}/auth/me/export")

    async def run(self, start: asyncio.Event, deadline: float):
        await start.wait()  # everyone logs in at the same instant
        await self.login()
        round_no = 0
        while time.monotonic() < deadline:
            await self.common()
            if self.account["role"] == "artisan":
                await self.artisan_round(round_no)
            else:
                await self.customer_round(round_no)
            round_no += 1
        await self.call("POST /auth/logout", "POST", "/api/v1/auth/logout",
                        json={"refresh_token": self.refresh_token})
        return round_no


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


async def run(base_url: str, duration: int) -> None:
    with open(CREDENTIALS_FILE) as f:
        accounts = json.load(f)
    stats = Stats()
    limits = httpx.Limits(max_connections=len(accounts) * 2, max_keepalive_connections=len(accounts) * 2)
    async with httpx.AsyncClient(base_url=base_url, timeout=60, limits=limits) as http:
        print(f"Warming up {base_url} ...")
        t = time.perf_counter()
        try:
            r = await http.get("/health", timeout=120)
            print(f"  /health {r.status_code} in {(time.perf_counter() - t):.1f}s: {r.text}")
        except httpx.HTTPError as e:
            raise SystemExit(f"API unreachable: {e}")

        start = asyncio.Event()
        began = time.monotonic()
        users = [VirtualUser(http, a, stats) for a in accounts]
        tasks = [asyncio.create_task(u.run(start, began + duration)) for u in users]
        print(f"Releasing {len(users)} users for {duration}s ...")
        start.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.monotonic() - began

    failures = [r for r in results if isinstance(r, Exception)]
    if stats.login_seconds:
        ls = stats.login_seconds
        print(f"\nTime until logged in: {len(ls)}/{len(users)} users got in; "
              f"p50 {pct(ls, 50):.1f}s, p95 {pct(ls, 95):.1f}s, slowest {max(ls):.1f}s")
    rounds = sum(r for r in results if isinstance(r, int))
    total = sum(len(v) for v in stats.latency.values())
    errors = sum(c for s in stats.status.values() for code, c in s.items() if code >= 400)

    print(f"\n{len(users)} users, {elapsed:.0f}s, {total} requests, {total / elapsed:.1f} req/s, "
          f"{rounds} full rounds, {errors} error responses ({100 * errors / max(total, 1):.1f}%), "
          f"{len(failures)} users aborted")
    for f in failures[:5]:
        print(f"  aborted: {f}")
    header = f"{'endpoint':42} {'n':>5} {'p50':>7} {'p95':>7} {'p99':>7} {'max':>7}  statuses"
    print("\n" + header + "\n" + "-" * len(header))
    for name in sorted(stats.latency, key=lambda n: -statistics.median(stats.latency[n])):
        lat = stats.latency[name]
        codes = " ".join(f"{c}x{k}" for k, c in sorted(stats.status[name].items()))
        print(f"{name:42} {len(lat):>5} {pct(lat, 50):>6.0f}ms {pct(lat, 95):>6.0f}ms "
              f"{pct(lat, 99):>6.0f}ms {max(lat):>6.0f}ms  {codes}")
    shown = [(n, c, b) for n, s in stats.samples.items() for c, b in s.items()]
    if shown:
        print("\nFirst error body per endpoint/status:")
        for n, c, b in shown[:25]:
            print(f"  {n} {c}: {b}")

    with open("loadtest_results.json", "w") as f:
        json.dump({
            "base_url": base_url, "users": len(users), "seconds": elapsed, "requests": total,
            "errors": errors,
            "endpoints": {
                n: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95), "p99": pct(v, 99), "max": max(v),
                    "statuses": dict(stats.status[n])}
                for n, v in stats.latency.items()
            },
        }, f, indent=2)
    print("\nFull results: loadtest_results.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["seed", "run", "cleanup"])
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--artisans", type=int, default=2)
    parser.add_argument("--base-url", default="https://kazihub-52ph.onrender.com")
    parser.add_argument("--duration", type=int, default=120, help="seconds of sustained traffic")
    args = parser.parse_args()
    if args.command == "seed":
        asyncio.run(seed(args.clients, args.artisans))
    elif args.command == "run":
        asyncio.run(run(args.base_url, args.duration))
    else:
        asyncio.run(cleanup())
