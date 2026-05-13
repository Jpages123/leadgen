"""Yellsa.com scraper — JS-rendered directory (Playwright)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Generator

from playwright.sync_api import sync_playwright

from app.scrapers.base import classify_business_type, infer_city_and_province, normalise_phone
from app.utils.logger import get_logger

log = get_logger(__name__)

BASE_URL = "https://www.yellsa.com"
SEARCH_URL = "https://www.yellsa.com/search/{category}/{city}"


@dataclass
class YellsaLead:
    business_name: str
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    city: str | None = None
    province: str | None = None
    business_type: str | None = None
    source_url: str | None = None
    source: str = "yellsa"


@dataclass
class YellsaSearchResult:
    category: str
    city: str
    leads: list[YellsaLead] = field(default_factory=list)
    pages_scraped: int = 0
    error: str | None = None


# ── Category slug mapping ─────────────────────────────────────────────────────
# Yellsa uses URL slugs like /business/hairdressers/
CATEGORY_MAP = {
    "hair salon": "hairdressers",
    "nail salon": "nail-salons",
    "beauty salon": "beauty-salons",
    "cleaning service": "cleaning-services",
    "photographer": "photographers",
    "event planner": "event-planners",
    "construction": "construction",
    "automotive": "auto-repair",
}

# SA cities to search
CITIES = [
    "cape-town", "johannesburg", "durban", "pretoria", "port-elizabeth",
    "centurion", "sandton", "randburg", "stellenbosch", "george",
]


def _search_page_urls(category: str, city: str, max_pages: int = 10) -> Generator[str, None, None]:
    """Yield search result page URLs for a category/city combination."""
    cat_slug = CATEGORY_MAP.get(category, category.lower().replace(" ", "-"))
    city_slug = city.lower().replace(" ", "-")
    base = f"{BASE_URL}/search/{cat_slug}/{city_slug}"
    yield base
    for page in range(2, max_pages + 1):
        yield f"{base}?page={page}"


def _parse_listing_page(html: str, search_url: str) -> Generator[YellsaLead, None, None]:
    """Parse a single yellsa search results page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Main listing container
    listings = soup.select("div.business-card, div.listing, article.listing, div.listing-card")
    if not listings:
        listings = soup.select("div[data-listing-id], div.listing-item")

    for listing in listings:
        # Business name
        name_el = listing.select_one("h2 a, h3 a, a.listing-name, div.name a")
        if not name_el:
            continue
        business_name = name_el.get_text(strip=True)

        # Profile URL
        profile_url = name_el.get("href", "")
        if profile_url and not profile_url.startswith("http"):
            profile_url = BASE_URL + profile_url

        # Phone
        phone_el = listing.select_one("a.phone, span.phone, div.phone a, a[href^='tel:']")
        phone = None
        if phone_el:
            phone = normalise_phone(phone_el.get_text())

        # Location
        loc_el = listing.select_one("address, div.address, span.city, div.location")
        city_raw = loc_el.get_text(strip=True) if loc_el else ""
        city, province = infer_city_and_province(city_raw)

        # Category
        cat_el = listing.select_one("span.category, div.category, a.category")
        categories = [cat_el.get_text(strip=True)] if cat_el else []
        biz_type = classify_business_type(business_name, categories)

        yield YellsaLead(
            business_name=business_name,
            phone=phone,
            city=city,
            province=province,
            business_type=biz_type,
            source_url=profile_url,
        )


def scrape_category_city(
    category: str,
    city: str,
    max_pages: int = 10,
    rate_limit: float = 2.0,
) -> YellsaSearchResult:
    """Scrape one category × city combination.

    Args:
        category: Business type (e.g. "hair salon")
        city: City slug (e.g. "cape-town")
        max_pages: Maximum result pages to scrape
        rate_limit: Seconds between page requests

    Returns:
        YellsaSearchResult with all leads found.
    """
    result = YellsaSearchResult(category=category, city=city)
    delay = 1.0 / max(rate_limit, 0.5)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-ZA",
        )

        for page_num, page_url in enumerate(_search_page_urls(category, city, max_pages)):
            try:
                page = context.new_page()
                response = page.goto(page_url, timeout=20_000)

                if response and response.status >= 400:
                    log.warning("yellsa_http_error", url=page_url, status=response.status)
                    page.close()
                    break

                # Wait for JS content to render
                page.wait_for_timeout(1500)

                html = page.content()
                leads = list(_parse_listing_page(html, page_url))
                result.leads.extend(leads)
                result.pages_scraped += 1

                log.info(
                    "yellsa_page_done",
                    category=category,
                    city=city,
                    page=page_num + 1,
                    leads_found=len(leads),
                )
                page.close()

                if not leads:
                    break  # No results on this page — stop pagination

                time.sleep(delay)

            except Exception as exc:
                log.error("yellsa_page_error", url=page_url, error=str(exc))
                if page:
                    page.close()
                break

        browser.close()

    return result


def run_yellsa_discovery(
    verticals: list[str] | None = None,
    cities: list[str] | None = None,
    max_pages: int = 10,
) -> list[YellsaSearchResult]:
    """Run yellsa discovery across all verticals × cities.

    Defaults to hair & beauty × 10 SA cities if no filters given.
    """
    verticals = verticals or ["hair salon", "nail salon", "beauty salon"]
    cities = cities or CITIES

    results = []
    for category in verticals:
        for city in cities:
            result = scrape_category_city(category, city, max_pages=max_pages)
            results.append(result)
            time.sleep(1.0)  # polite delay between searches

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    cat = sys.argv[1] if len(sys.argv) > 1 else "hair salon"
    city = sys.argv[2] if len(sys.argv) > 2 else "cape-town"

    print(f"Scraping {cat} in {city}...")
    result = scrape_category_city(cat, city)
    print(f"Pages scraped: {result.pages_scraped}")
    print(f"Leads found: {len(result.leads)}")
    for lead in result.leads[:5]:
        print(f"  - {lead.business_name} | {lead.phone} | {lead.city} | {lead.province}")