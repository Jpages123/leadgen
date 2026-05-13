"""Lead scrapers package."""
from app.scrapers.base import infer_city_and_province, normalise_phone
from app.scrapers.cylex import (
    CylexLead,
    CylexSearchResult,
    scrape_category_city as scrape_cylex,
    run_cylex_discovery,
)
from app.scrapers.yellsa import (
    YellsaLead,
    YellsaSearchResult,
    scrape_category_city as scrape_yellsa,
    run_yellsa_discovery,
)

__all__ = [
    "YellsaLead",
    "YellsaSearchResult",
    "scrape_yellsa",
    "run_yellsa_discovery",
    "CylexLead",
    "CylexSearchResult",
    "scrape_cylex",
    "run_cylex_discovery",
    "normalise_phone",
    "infer_city_and_province",
]