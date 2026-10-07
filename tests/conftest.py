import os

# Tests must never reach a real database: point the app's settings at an
# address nothing listens on, before anything imports app.core.config.
# (Environment variables take precedence over .env.)
os.environ["MONGODB_URL"] = "mongodb://127.0.0.1:1/kazihub_tests_never_connect"
os.environ.setdefault("SECRET_KEY", "test-secret")

import mongomock
import mongomock.filtering
import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import DBRef
from mongomock_motor import AsyncMongoMockClient

# mongomock 4.3.0's Database.list_collection_names() doesn't yet accept the
# newer pymongo 4.17 keyword args (e.g. authorizedCollections) that Beanie's
# collection-existence check passes through. This only affects the in-memory
# test double, not the real Motor/Atlas connection used by the app.
_original_list_collection_names = mongomock.Database.list_collection_names


def _list_collection_names_compat(self, filter=None, session=None, **_ignored):
    return _original_list_collection_names(self, filter=filter, session=session)


mongomock.Database.list_collection_names = _list_collection_names_compat

# mongomock stores Beanie Link fields as bson.DBRef objects, but its dotted
# key resolver (used for queries like {"user.$id": ...}, which this codebase
# uses throughout to query Link fields) only descends into plain dicts and
# gives up on a DBRef, silently returning no match. Real MongoDB stores a
# DBRef as an embedded {$ref, $id} document, so "field.$id" works there with
# no special-casing — this patch makes the in-memory test double behave the
# same way, rather than changing the (correct) production query pattern.
_original_iter_key_candidates = mongomock.filtering.iter_key_candidates


def _iter_key_candidates_with_dbref_support(key, doc):
    if isinstance(doc, DBRef):
        doc = doc.as_doc()
    return _original_iter_key_candidates(key, doc)


mongomock.filtering.iter_key_candidates = _iter_key_candidates_with_dbref_support

# mongomock's Collection.create_indexes() (the plural form Beanie calls with
# a list of pymongo IndexModel objects) hardcodes which options it forwards
# to create_index() — unique/sparse/expireAfterSeconds/name, but NOT
# partialFilterExpression. That option is silently dropped, so a partial
# unique index (e.g. User.nin_hash, unique only when the field is an actual
# string) behaves as a plain unique index in tests: every document without
# the field collides on its shared null value. Real MongoDB has no such gap
# — this only patches the in-memory test double.
from mongomock.collection import Collection as _MongomockCollection


def _create_indexes_with_partial_filter_support(self, indexes, session=None):
    results = []
    for index in indexes:
        results.append(
            self.create_index(
                index.document["key"].items(),
                session=session,
                expireAfterSeconds=index.document.get("expireAfterSeconds"),
                unique=index.document.get("unique", False),
                sparse=index.document.get("sparse", False),
                name=index.document.get("name"),
                partialFilterExpression=index.document.get("partialFilterExpression"),
            )
        )
    return results


_MongomockCollection.create_indexes = _create_indexes_with_partial_filter_support

from app.models.audit_log import AuditLog
from app.models.bank_account import BankAccount
from app.models.booking import Booking
from app.models.booking_status_history import BookingStatusHistory
from app.models.chat import Conversation, Message
from app.models.dispute import Dispute
from app.models.favorite import SavedProfessional
from app.models.gig import Gig
from app.models.idempotency_key import IdempotencyRecord
from app.models.notification import Notification
from app.models.pending_user import PendingUser
from app.models.portfolio import PortfolioItem
from app.models.profile import Profile
from app.models.review import Review
from app.models.service import Service
from app.models.session import UserSession
from app.models.push_subscription import PushSubscription
from app.models.support_ticket import SupportTicket
from app.models.transaction import Transaction
from app.models.user import User
from app.models.user_role import UserRole
from app.models.verification import Verification
from app.models.webhook_event import ProcessedWebhookEvent


@pytest_asyncio.fixture(autouse=True)
async def init_test_db():
    """Fresh in-memory Mongo (mongomock) per test, with every collection
    registered so relationship/link fields resolve correctly."""
    client = AsyncMongoMockClient(tz_aware=True)
    await init_beanie(
        database=client["kazihub_test"],
        document_models=[
            User,
            UserRole,
            Profile,
            Service,
            PortfolioItem,
            Gig,
            Verification,
            PendingUser,
            Conversation,
            Message,
            Booking,
            BookingStatusHistory,
            Dispute,
            Review,
            Notification,
            AuditLog,
            SavedProfessional,
            BankAccount,
            Transaction,
            ProcessedWebhookEvent,
            UserSession,
            IdempotencyRecord,
            SupportTicket,
            PushSubscription,
        ],
    )
    yield


@pytest.fixture(autouse=True)
def stub_paystack_transfer_network_call(monkeypatch):
    """No test should ever hit the real Paystack API. `request_artisan_payout`
    still runs its real logic (bank-account lookup, reference generation);
    only the actual network call is replaced. Patched at
    app.services.payouts (where it's imported into, via `from ... import`)
    rather than app.services.paystack (where it's defined), since a
    `from module import name` binding isn't affected by patching the
    original module's attribute afterward."""

    async def _fake_initiate_transfer(amount_kobo, recipient_code, reason, reference):
        return {"transfer_code": "TRF_test_stub", "reference": reference, "status": "pending"}

    monkeypatch.setattr(
        "app.services.payouts.initiate_transfer", _fake_initiate_transfer
    )
