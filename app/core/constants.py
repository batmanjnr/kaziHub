# app/core/constants.py
"""Closed value sets shared by the API and the frontend. Spelling and
capitalisation are part of the contract: the frontend filters and compares
on these exact strings (frontend asks 3, 20, 37)."""

CATEGORIES = (
    "Electricians",
    "Plumbers",
    "Carpenters",
    "AC Technicians",
    "Appliance Repair Specialists",
    "Mechanics",
    "Solar Installers",
    "CCTV Installers",
    "Painters",
    "Welders",
    "Cleaners",
    "Tutors",
    "Tailors",
    "Hair Stylists",
    "Photographers",
    "Event Professionals",
)

NIGERIAN_STATES = (
    "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue",
    "Borno", "Cross River", "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu",
    "Gombe", "Imo", "Jigawa", "Kaduna", "Kano", "Katsina", "Kebbi", "Kogi",
    "Kwara", "Lagos", "Nasarawa", "Niger", "Ogun", "Ondo", "Osun", "Oyo",
    "Plateau", "Rivers", "Sokoto", "Taraba", "Yobe", "Zamfara", "Abuja (FCT)",
)

RESPONSE_TIMES = (
    "Within 15 minutes",
    "Within 30 minutes",
    "Within 1 hour",
    "Within 3 hours",
    "Within 6 hours",
    "Within a day",
)

DURATION_ESTIMATES = (
    "Under 1 hr", "1 hr", "1-2 hrs", "2-3 hrs", "3-5 hrs",
    "Half a day", "Full day", "2-3 days", "1 week+",
)

LANGUAGES = ("en", "yo", "ig", "ha", "fr")
THEMES = ("light", "dark", "system")

# Legacy free-text language labels already stored on accounts, mapped to
# the codes above (ask 20). Matched case-insensitively on the label's start.
LANGUAGE_LABEL_PREFIXES = {
    "english": "en",
    "yoruba": "yo",
    "igbo": "ig",
    "hausa": "ha",
    "french": "fr",
    "français": "fr",
    "francais": "fr",
}

PHONE_VISIBILITY_OPTIONS = ("after_escrow", "verified_only", "hidden")

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
