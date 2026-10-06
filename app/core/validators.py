# app/core/validators.py
"""Field rules shared by request schemas (frontend ask 37). Each is an
Annotated type, so a violation becomes a normal FastAPI 422 naming the
field — the same rules the frontend already applies in its forms, enforced
here so a direct API call can't save anything else."""
import re
from datetime import date
from typing import Annotated, List, Optional

from pydantic import AfterValidator, BeforeValidator, Field

from app.core.constants import (
    CATEGORIES,
    DURATION_ESTIMATES,
    LANGUAGES,
    NIGERIAN_STATES,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    PHONE_VISIBILITY_OPTIONS,
    RESPONSE_TIMES,
    THEMES,
)

_NAME_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ .'\-]{0,39}$")
PHONE_RE = re.compile(r"^\+234[789]\d{9}$")


def _one_of(allowed, label: str):
    def check(value):
        if value is not None and value not in allowed:
            raise ValueError(f"{label} must be one of: {', '.join(allowed)}")
        return value
    return check


def _strip(value):
    return value.strip() if isinstance(value, str) else value


def _check_name(value: str) -> str:
    if not _NAME_RE.match(value):
        raise ValueError("Use 1-40 letters, spaces, hyphens, apostrophes or full stops.")
    return value


def normalize_phone(value):
    """Accepts +234XXXXXXXXXX, 234XXXXXXXXXX or 0XXXXXXXXXX (older
    accounts) and returns the canonical +234 form."""
    if not isinstance(value, str):
        return value
    digits = re.sub(r"[\s\-()]", "", value)
    if digits.startswith("0") and len(digits) == 11:
        digits = "+234" + digits[1:]
    elif digits.startswith("234") and len(digits) == 13:
        digits = "+" + digits
    return digits


def _check_phone(value: str) -> str:
    if not PHONE_RE.match(value):
        raise ValueError("Phone must be +234 followed by 10 digits starting with 7, 8 or 9.")
    return value


def _check_skills(value: Optional[List[str]]):
    if value is None:
        return value
    cleaned = [s.strip() for s in value]
    if len(cleaned) > 12:
        raise ValueError("At most 12 skills.")
    if any(not 1 <= len(s) <= 30 for s in cleaned):
        raise ValueError("Each skill must be 1-30 characters.")
    if len({s.lower() for s in cleaned}) != len(cleaned):
        raise ValueError("Skills must not repeat.")
    return cleaned


def _not_future(value: Optional[date]):
    if value is not None and value > date.today():
        raise ValueError("Date can't be in the future.")
    return value


def _empty_to_none(value):
    if isinstance(value, str) and not value.strip():
        return None
    return value


Name = Annotated[str, BeforeValidator(_strip), AfterValidator(_check_name)]
Phone = Annotated[str, BeforeValidator(normalize_phone), AfterValidator(_check_phone)]
State = Annotated[str, BeforeValidator(_strip), AfterValidator(_one_of(NIGERIAN_STATES, "state"))]
Category = Annotated[str, BeforeValidator(_strip), AfterValidator(_one_of(CATEGORIES, "category"))]
Language = Annotated[str, AfterValidator(_one_of(LANGUAGES, "preferred_language"))]
Theme = Annotated[str, BeforeValidator(lambda v: v.lower() if isinstance(v, str) else v), AfterValidator(_one_of(THEMES, "theme"))]
ResponseTime = Annotated[str, AfterValidator(_one_of(RESPONSE_TIMES, "response_time"))]
DurationEstimate = Annotated[str, AfterValidator(_one_of(DURATION_ESTIMATES, "duration_estimate"))]
PhoneVisibility = Annotated[str, AfterValidator(_one_of(PHONE_VISIBILITY_OPTIONS, "phone_visibility"))]
Password = Annotated[
    str,
    Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        description=f"{PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters.",
    ),
]
Tagline = Annotated[str, BeforeValidator(_empty_to_none), Field(max_length=60)]
Bio = Annotated[str, BeforeValidator(_empty_to_none), Field(max_length=500)]
Neighborhood = Annotated[str, BeforeValidator(_empty_to_none), Field(max_length=50)]
Skills = Annotated[List[str], AfterValidator(_check_skills)]
YearsOfExperience = Annotated[int, Field(ge=0, le=60)]
PastDate = Annotated[date, AfterValidator(_not_future)]

# Per-document-type ID formats (ask 37), checked in VerificationSubmit.
DOCUMENT_NUMBER_RULES = {
    "nin": (re.compile(r"^\d{11}$"), "NIN must be 11 digits."),
    "passport": (re.compile(r"^[A-Za-z]\d{8}$"), "Passport number must be a letter followed by 8 digits."),
    "voters_card": (re.compile(r"^[A-Za-z0-9]{19}$"), "Voter's card number must be 19 characters."),
    "drivers_license": (
        re.compile(r"^[A-Za-z]{3}[A-Za-z0-9]{8,9}$"),
        "Driver's licence number must be 3 letters followed by 8-9 characters.",
    ),
}
