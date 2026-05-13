"""Shared utilities for all lead scrapers."""
from __future__ import annotations

import re
from typing import Optional

# SA provinces
SA_PROVINCES = {
    "western cape", "cape town", "kwazulu-natal", "kzn", "gauteng",
    "joburg", "johannesburg", "pretoria", "north west", "limpopo",
    "mpumalanga", "eastern cape", "free state",
}

# City → province mapping for inference
CITY_TO_PROVINCE = {
    "cape town": "Western Cape",
    "johannesburg": "Gauteng",
    "durban": "KwaZulu-Natal",
    "pretoria": "Gauteng",
    "port elizabeth": "Eastern Cape",
    "centurion": "Gauteng",
    "sandton": "Gauteng",
    "randburg": "Gauteng",
    "stellenbosch": "Western Cape",
    "paarl": "Western Cape",
    "george": "Western Cape",
    "bloemfontein": "Free State",
    "nelspruit": "Mpumalanga",
    "polokwane": "Limpopo",
}


def normalise_phone(raw: str) -> Optional[str]:
    """Convert any SA phone number to E.164 +27XXXXXXXXX format, or return None.

    Handles:
      0821234567  →  +27821234567
      +27821234567 → +27821234567
      +27712345678 → +27712345678
      0211234567   →  +27211234567
    """
    digits = re.sub(r"[^0-9]", "", str(raw))

    # 11-digit starting with 27: already E.164 without the +
    if len(digits) == 11 and digits.startswith("27"):
        return f"+{digits}"

    # 10-digit starting with 0: local SA number
    if len(digits) == 10 and digits[0] == "0":
        return f"+27{digits[1:]}"

    # 9-digit (no leading 0 or 27): prepend +27
    if len(digits) == 9 and not digits.startswith("27"):
        return f"+27{digits}"

    return None


def normalise_email(raw: str) -> Optional[str]:
    """Basic email validation + lowercase."""
    raw = raw.strip().lower()
    if re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", raw):
        return raw
    return None


def infer_city_and_province(raw_location: str) -> tuple[Optional[str], Optional[str]]:
    """Parse a location string into city + province.

    Handles: 'Cape Town, Western Cape', 'Cape Town', 'Cape Town, WC'
    """
    if not raw_location:
        return None, None

    location = raw_location.strip()
    # Remove trailing region codes: ", WC", ", KZN"
    location = re.sub(r",\s*(WC|EC|GP|MP|KZN|FS|NW|LP)\s*$", "", location, flags=re.IGNORECASE)

    parts = [p.strip() for p in location.split(",")]
    city = parts[0] if parts else None

    # Direct city lookup
    city_lower = city.lower() if city else ""
    province = CITY_TO_PROVINCE.get(city_lower)

    # Use second part as province
    if not province and len(parts) > 1:
        province = parts[1].strip().title()

    # City name itself is a province name
    if not province and city_lower in SA_PROVINCES:
        province = city.title()

    return city, province


def classify_business_type(business_name: str, categories: list[str] | None = None) -> Optional[str]:
    """Heuristic classification based on name and category hints.

    Order matters — more specific types come before general ones to avoid
    substring collisions (e.g. 'cleaning services' must beat 'beauty').
    """
    name = business_name.lower()

    # Most-specific-first ordering
    type_keywords = [
        ("cleaning service",  ["cleaning service", "cleaning company", "house cleaning", "maid service", "cleaners", "cleaning"]),
        ("beauty salon",      ["beauty salon", "beauty spa", "beauty clinic", "beauty", "skin care", "aesthetics"]),
        ("hair salon",        ["hair salon", "hairdresser", "hairdressing", "barber shop", "barbershop", "hair"]),
        ("nail salon",        ["nail salon", "nail bar", "manicure", "pedicure", "gel nails", "nail art", "nail"]),
        ("photographer",      ["photographer", "photography", "photo studio", "photo shoot", "photo"]),
        ("event planner",     ["event planner", "event planning", "wedding planner", "wedding planning", "event"]),
        ("construction",      ["construction", "building contractor", "renovations", "builder", "building"]),
        ("automotive",        ["automotive", "auto repair", "car mechanic", "panel beater", "tyres", "auto"]),
        ("restaurant",        ["restaurant", "cafe", "bistro", "coffee shop"]),
        ("consulting",        ["consulting", "consultant"]),
        ("fitness",           ["fitness", "gym", "personal trainer", "yoga studio", "fitness"]),
    ]

    for biz_type, kws in type_keywords:
        if any(kw in name for kw in kws):
            return biz_type

    if categories:
        cat_str = " ".join(categories).lower()
        for biz_type, kws in type_keywords:
            if any(kw in cat_str for kw in kws):
                return biz_type

    return None


def extract_website_from_text(text: str) -> Optional[str]:
    """Extract a website URL from raw text."""
    match = re.search(r"https?://[^\s<>'\"]+", text)
    return match.group(0) if match else None