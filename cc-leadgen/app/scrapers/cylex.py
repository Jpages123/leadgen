"""Cylex.co.za scraper — static(ish) directory (BeautifulSoup)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Generator

import requests

from app.scrapers.base import classify_business_type, infer_city_and_province, normalise_phone
from app.utils.logger import get_logger

log = get_logger(__name__)

BASE_URL = "https://www.cylex.co.za"


@dataclass
class CylexLead:
    business_name: str
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    city: str | None = None
    province: str | None = None
    business_type: str | None = None
    source_url: str | None = None
    source: str = "cylex"


@dataclass
class CylexSearchResult:
    category: str
    city: str
    leads: list[CylexLead] = field(default_factory=list)
    pages_scraped: int = 0
    error: str | None = None


# Cylex SA category pages
CATEGORY_MAP = {
    "hair salon": "hairdressers",
    "nail salon": "nail-salons",
    "beauty salon": "beauty-salons",
    "cleaning service": "cleaning-services",
    "photographer": "photographers",
    "event planner": "event-planners",
}

CITIES = [
    "cape-town", "johannesburg", "durban", "pretoria", "port-elizabeth",
    "centurion", "sandton", "randburg", "stellenbosch", "george",
]


def _build_search_url(category: str, city: str, page: int = 1) -> str:
    cat_slug = CATEGORY_MAP.get(category, category.lower().replace(" ", "-"))
    city_slug = city.lower().replace(" ", "-")
    base = f"{BASE_URL}/{cat_slug}/{city_slug}.html"
    if page > 1:
        base = base.replace(".html", f"_{page}.html")
    return base


def _parse_page(html: str, page_url: str) -> Generator[CylexLead, None, None]:
    """Parse a Cylex search results page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Main listing selector — try common patterns
    listings = soup.select("div.company_data, div.company_list_item, article.company")
    if not listings:
        listings = soup.select("div.result-item, div.listing-container, li.company")

    for listing in listings:
        # Business name — usually in h2 or h3 inside listing
        name_el = (
            listing.select_one("h2 a, h3 a, h2, h3")
            or listing.select_one("div.company-name a, span.company-name")
        )
        if not name_el:
            continue
        name_text = name_el.get_text(strip=True)
        if not name_text or len(name_text) < 2:
            continue
        business_name = name_text

        # Profile URL
        if name_el.name == "a":
            profile_url = name_el.get("href", "")
        else:
            link_el = listing.select_one("h2 a, h3 a, a.company-link")
            profile_url = link_el.get("href", "") if link_el else ""

        if profile_url and not profile_url.startswith("http"):
            profile_url = BASE_URL + profile_url

        # Phone — cylex uses tel: links or data attributes
        phone_el = listing.select_one(
            "a[href^='tel:'], span.phone a, div.phone, "
            "span[data-phone], a.showPhoneNo, div.phone_number"
        )
        phone = None
        if phone_el:
            raw = phone_el.get("href", "") or phone_el.get("data-phone", "") or phone_el.get_text()
            phone = normalise_phone(raw)

        # Email — sometimes in a mailto: or data attribute
        email_el = listing.select_one(
            "a[href^='mailto:'], span.email, div.email"
        )
        email = None
        if email_el:
            href = email_el.get("href", "")
            if href.startswith("mailto:"):
                email = href.replace("mailto:", "").split("?")[0].strip()
            else:
                email = email_el.get_text(strip=True)

        # Website
        website_el = listing.select_one(
            "a[href^='http']:not([href*='cylex'])"
        )
        website = None
        if website_el:
            href = website_el.get("href", "")
            if href.startswith("http") and "cylex" not in href:
                website = href

        # Location / city
        loc_el = listing.select_one("address, div.address, span.city, span.location, div.city")
        city_raw = loc_el.get_text(strip=True) if loc_el else ""
        city, province = infer_city_and_province(city_raw)

        # Category hints
        cat_el = listing.select_one("span.category, a.category, div.category")
        categories = [cat_el.get_text(strip=True)] if cat_el else []
        biz_type = classify_business_type(business_name, categories)

        yield CylexLead(
            business_name=business_name,
            phone=phone,
            email=email,
            website=website,
            city=city,
            province=province,
            business_type=biz_type,
            source_url=profile_url or None,
        )


def scrape_category_city(
    category: str,
    city: str,
    max_pages: int = 10,
    rate_limit: float = 2.0,
) -> CylexSearchResult:
    """Scrape one category × city combination.

    Returns:
        CylexSearchResult with all leads found.
    """
    result = CylexSearchResult(category=category, city=city)
    delay = 1.0 / max(rate_limit, 0.5)

    for page in range(1, max_pages + 1):
        url = _build_search_url(category, city, page)

        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-ZA,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=15,
            )
        except Exception as exc:
            log.error("cylex_request_failed", url=url, error=str(exc))
            break

        if resp.status_code >= 400:
            log.warning("cylex_http_error", url=url, status=resp.status_code)
            break

        leads = list(_parse_page(resp.text, url))
        result.leads.extend(leads)
        result.pages_scraped += 1

        log.info(
            "cylex_page_done",
            category=category,
            city=city,
            page=page,
            leads_found=len(leads),
        )

        if not leads:
            break  # No more results

        time.sleep(delay)

    return result


def run_cylex_discovery(
    verticals: list[str] | None = None,
    cities: list[str] | None = None,
    max_pages: int = 10,
) -> list[CylexSearchResult]:
    """Run cylex discovery across verticals × cities."""
    verticals = verticals or ["hair salon", "nail salon", "beauty salon"]
    cities = cities or CITIES

    results = []
    for category in verticals:
        for city in cities:
            result = scrape_category_city(category, city, max_pages=max_pages)
            results.append(result)
            time.sleep(1.0)

    return results


if __name__ == "__main__":
    import sys

    cat = sys.argv[1] if len(sys.argv) > 1 else "hair salon"
    city = sys.argv[2] if len(sys.argv) > 2 else "cape-town"

    print(f"Scraping {cat} in {city}...")
    result = scrape_category_city(cat, city)
    print(f"Pages scraped: {result.pages_scraped}")
    print(f"Leads found: {len(result.leads)}")
    for lead in result.leads[:5]:
        print(f"  - {lead.business_name} | {lead.phone} | {lead.city}")