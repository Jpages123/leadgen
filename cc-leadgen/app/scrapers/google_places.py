"""Google Places API scraper — replaces yellsa + cylex.
Uses the Places API Text Search (New) endpoint which returns business name,
phone, website, rating, review_count, and location in a single call.
Cost: ~/usr/bin/zsh.032 per request (Text Search New). Each call returns up to 20 results.
We use nextPageToken to page through up to 3 pages (60 results) per vertical/city.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional
import httpx
from app.config import get_settings
from app.scrapers.base import (
    classify_business_type,
    infer_city_and_province,
    normalise_phone,
)
from app.utils.logger import get_logger
log = get_logger(__name__)
# ── SA city search strings (used as-is in the API query) ──────────────────────
SA_CITIES = [
    "Cape Town",
    "Johannesburg",
    "Durban",
    "Pretoria",
    "Port Elizabeth",
    "Bloemfontein",
    "East London",
    "Nelspruit",
    "Polokwane",
]
# ── Target verticals (priority order from WEB_REVAMP_ENGINE.md) ───────────────
DEFAULT_VERTICALS = [
    "plumber",
    "electrician",
    "builder",
    "cleaning service",
    "photographer",
    "event planner",
    "car wash",
    "mechanic",
]
# Google Places API base URL
_PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
# Fields we need — minimises cost (only pay for requested field masks)
_FIELD_MASK = ",".join([
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.primaryType",
    "places.shortFormattedAddress",
    "places.addressComponents",
    "nextPageToken",
])
@dataclass
class GooglePlacesLead:
    business_name: str
    phone: Optional[str] = None
    website: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    business_type: Optional[str] = None
    google_rating: Optional[float] = None
    google_review_count: Optional[int] = None
    source_url: Optional[str] = None
@dataclass
class GooglePlacesResult:
    vertical: str
    city: str
    leads: list[GooglePlacesLead] = field(default_factory=list)
    pages_fetched: int = 0
    error: Optional[str] = None
def _extract_city_province(components: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Extract city and province from addressComponents array."""
    city = None
    province = None
    for comp in components:
        types = comp.get("types", [])
        if "locality" in types or "sublocality" in types:
            city = comp.get("longText")
        if "administrative_area_level_1" in types:
            province = comp.get("longText")
    return city, province
def _parse_place(place: dict, vertical: str) -> Optional[GooglePlacesLead]:
    """Parse a single place dict from the API response."""
    name = (place.get("displayName") or {}).get("text") or place.get("name")
    if not name:
        return None
    # Phone: prefer international format
    phone_raw = place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber")
    phone = normalise_phone(phone_raw) if phone_raw else None
    website = place.get("websiteUri")
    # Strip tracking params / normalise
    if website and "?" in website:
        website = website.split("?")[0].rstrip("/")
    rating = place.get("rating")
    review_count = place.get("userRatingCount")
    # City/province from addressComponents (most reliable)
    address_components = place.get("addressComponents", [])
    city, province = _extract_city_province(address_components)
    # Fallback: parse shortFormattedAddress
    if not city:
        short_addr = place.get("shortFormattedAddress") or place.get("formattedAddress") or ""
        city, province = infer_city_and_province(short_addr)
    business_type = classify_business_type(
        name,
        categories=[place.get("primaryType", ""), vertical],
    )
    return GooglePlacesLead(
        business_name=name,
        phone=phone,
        website=website,
        city=city,
        province=province,
        business_type=business_type,
        google_rating=float(rating) if rating is not None else None,
        google_review_count=int(review_count) if review_count is not None else None,
        source_url=None,
    )
def search_places(
    *,
    vertical: str,
    city: str,
    api_key: str,
    max_pages: int = 3,
    rate_limit_delay: float = 0.5,
) -> GooglePlacesResult:
    """Run a Text Search for a vertical + city combo.
    Args:
        vertical: Search term e.g. 'plumber'
        city: SA city e.g. 'Cape Town'
        api_key: Google Places API key
        max_pages: Max pagination loops (each page = 20 results, ~/usr/bin/zsh.032)
        rate_limit_delay: Seconds to wait between paged requests
    Returns:
        GooglePlacesResult with leads list
    """
    result = GooglePlacesResult(vertical=vertical, city=city)
    query = f"{vertical} in {city}, South Africa"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    payload: dict = {
        "textQuery": query,
        "languageCode": "en",
        "regionCode": "ZA",
        "maxResultCount": 20,
    }
    page = 0
    next_page_token: Optional[str] = None
    while page < max_pages:
        if next_page_token:
            payload["pageToken"] = next_page_token
        elif page > 0:
            break  # no more pages
        try:
            resp = httpx.post(
                _PLACES_URL,
                headers=headers,
                json=payload,
                timeout=15.0,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            result.error = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            log.error(
                "google_places_http_error",
                vertical=vertical,
                city=city,
                status=exc.response.status_code,
                body=exc.response.text[:200],
            )
            break
        except httpx.RequestError as exc:
            result.error = f"Request error: {exc}"
            log.error("google_places_request_error", vertical=vertical, city=city, error=str(exc))
            break
        data = resp.json()
        places = data.get("places", [])
        next_page_token = data.get("nextPageToken")
        for place in places:
            lead = _parse_place(place, vertical)
            if lead:
                result.leads.append(lead)
        result.pages_fetched += 1
        page += 1
        log.info(
            "google_places_page_done",
            vertical=vertical,
            city=city,
            page=page,
            leads_this_page=len(places),
            has_next=bool(next_page_token),
        )
        if not next_page_token:
            break
        if page < max_pages:
            time.sleep(rate_limit_delay)
    return result
def run_google_places_discovery(
    verticals: list[str] | None = None,
    cities: list[str] | None = None,
    max_pages: int = 3,
) -> list[GooglePlacesResult]:
    """Run discovery across all verticals x cities.
    Mirrors the interface of run_yellsa_discovery() / run_cylex_discovery()
    so the discovery worker can call it as a drop-in replacement.
    """
    settings = get_settings()
    api_key = settings.google_places_api_key
    if not api_key:
        log.error("google_places_no_api_key")
        return [
            GooglePlacesResult(
                vertical="all",
                city="all",
                error="GOOGLE_PLACES_API_KEY not set",
            )
        ]
    verticals = verticals or DEFAULT_VERTICALS
    cities = cities or SA_CITIES
    rate_limit_delay = 1.0 / max(1, settings.google_maps_rate_limit)
    results: list[GooglePlacesResult] = []
    for vertical in verticals:
        for city in cities:
            log.info("google_places_discovery_start", vertical=vertical, city=city)
            result = search_places(
                vertical=vertical,
                city=city,
                api_key=api_key,
                max_pages=max_pages,
                rate_limit_delay=rate_limit_delay,
            )
            results.append(result)
            # Polite delay between combos
            time.sleep(rate_limit_delay)
    return results
