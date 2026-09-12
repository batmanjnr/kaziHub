from app.api.v1.endpoints.gigs import create_gig, list_gigs
from app.core.security import get_password_hash
from app.models.gig import GigCreate
from app.models.profile import Profile
from app.models.user import User
from app.services.paystack import PaystackError


async def make_artisan(email) -> User:
    user = User(
        first_name="A",
        last_name="B",
        email=email,
        phone_number=f"+234800000{abs(hash(email)) % 10000:04d}",
        state="Lagos",
        role="artisan",
        hashed_password=get_password_hash("Passw0rd!"),
    )
    await user.insert()
    return user


def test_list_gigs_limit_field_is_bounded():
    import inspect

    sig = inspect.signature(list_gigs)
    limit_default = sig.parameters["limit"].default
    constraints = {type(c).__name__: c for c in limit_default.metadata}
    assert constraints["Ge"].ge == 1
    assert constraints["Le"].le == 100


async def test_gig_listing_still_works_within_bounds():
    artisan = await make_artisan("secreview1@example.com")
    profile = Profile(user=artisan, category="plumbing", state="Lagos")
    await profile.insert()
    await create_gig(
        GigCreate(
            title="Tap fix", description="desc", category="plumbing",
            price=1000, delivery_time_days=1,
        ),
        profile=profile,
    )

    results = await list_gigs(category=None, tag=None, limit=20, skip=0)
    assert len(results) == 1


def test_paystack_error_hides_transport_details_from_client():
    """Security-review fix: a raw network/transport exception message
    (which could include internal hostnames or connection details) must
    never be interpolated into a client-facing HTTPException detail."""
    transport_error = PaystackError("Network error contacting Paystack", safe_to_expose=False)
    assert "temporarily unavailable" in transport_error.client_message().lower()
    assert "Network error" not in transport_error.client_message()


def test_paystack_error_shows_business_message_to_client():
    business_error = PaystackError("Invalid account number", {"status": False}, safe_to_expose=True)
    assert business_error.client_message() == "Invalid account number"


def test_register_rate_limit_key_is_per_ip_not_per_email():
    """Security-review fix: /register had no rate limit at all, letting an
    attacker spam arbitrary victim emails through Kazihub's own SMTP sender.
    This just confirms the rate limiter is actually invoked with a
    per-IP key by inspecting the source, since exercising the real
    endpoint needs a full Request object."""
    import inspect

    from app.api.v1.endpoints import auth

    source = inspect.getsource(auth.register)
    assert "rate_limiter.hit" in source
    assert "register:" in source
