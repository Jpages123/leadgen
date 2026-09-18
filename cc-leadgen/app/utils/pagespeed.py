"""Google PageSpeed Insights API v5 wrapper.

Fetches Performance, SEO, Accessibility, and Best Practices scores for both
mobile and desktop strategies in a single function call.

Cost: FREE — PageSpeed Insights API has no usage charge and requires no billing.
The same API key used for Google Places works here.

Docs: https://developers.google.com/speed/docs/insights/v5/get-pagespeed
"""
from __future__ import annotations

import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

_PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Categories we request (each adds to response but no extra cost)
_CATEGORIES = ["performance", "seo", "accessibility", "best-practices"]


@dataclass
class PageSpeedScores:
    url: str
    # Mobile scores (0-100, None if API failed)
    mobile_performance: Optional[int] = None
    mobile_seo: Optional[int] = None
    mobile_accessibility: Optional[int] = None
    mobile_best_practices: Optional[int] = None
    # Desktop scores (0-100, None if API failed)
    desktop_performance: Optional[int] = None
    desktop_seo: Optional[int] = None
    desktop_accessibility: Optional[int] = None
    desktop_best_practices: Optional[int] = None
    # Per-strategy error tracking (None = strategy succeeded). A non-None
    # value means the audit for that strategy failed (timeout, HTTP error, etc).
    mobile_error: Optional[str] = None
    desktop_error: Optional[str] = None
    # Convenience: True if any strategy failed. UI / callers should treat
    # this as "audit incomplete, do not use partial scores".
    had_failure: bool = False


def _fetch_strategy(url: str, strategy: str, api_key: str, max_retries: int = 1, retry_delay: float = 2.0) -> dict | None:
    """Fetch PageSpeed results for one strategy (mobile or desktop).

    Returns the parsed JSON dict, or None on error.

    PageSpeed Lighthouse runs can take 40-55s on heavy WP+Elementor sites
    (it simulates a throttled mobile CPU and slow 3G network). 60s timeout
    with a single retry covers transient network blips and most slow sites
    without giving up on legitimately busy ones.
    """
    params = {
        "url": url,
        "strategy": strategy,
        "key": api_key,
        "category": _CATEGORIES,
    }
    last_err = None
    for attempt in range(1, max_retries + 2):  # initial + N retries
        try:
            resp = httpx.get(
                _PAGESPEED_URL,
                params=params,
                timeout=60.0,  # was 30s — heavy WP+Elementor needs 40-55s
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            log.warning(
                "pagespeed_http_error",
                url=url, strategy=strategy,
                status=exc.response.status_code,
                body=exc.response.text[:200],
            )
            return None  # HTTP errors are not retried
        except httpx.ReadTimeout as exc:
            last_err = f"ReadTimeout (attempt {attempt}/{max_retries + 1}): {exc}"
            if attempt <= max_retries:
                log.warning(
                    "pagespeed_read_timeout_retry",
                    url=url, strategy=strategy,
                    attempt=attempt, next_in_s=retry_delay,
                )
                _time.sleep(retry_delay)
                continue
            log.warning(
                "pagespeed_request_error",
                url=url, strategy=strategy,
                error=last_err,
            )
            return None
        except httpx.RequestError as exc:
            log.warning("pagespeed_request_error", url=url, strategy=strategy, error=str(exc))
            return None
    return None


def _extract_score(data: dict, category: str) -> Optional[int]:
    """Pull the 0-100 integer score for a category from the Lighthouse result."""
    try:
        raw = data["lighthouseResult"]["categories"][category]["score"]
        if raw is None:
            return None
        return int(round(float(raw) * 100))
    except (KeyError, TypeError, ValueError):
        return None


def fetch_pagespeed(url: str, api_key: str | None = None) -> PageSpeedScores:
    """Fetch PageSpeed Insights scores for a URL (mobile + desktop).

    Mobile and desktop strategies are fetched in parallel via ThreadPoolExecutor,
    cutting wall-clock time roughly in half (~30s saved on slow WP sites).

    Args:
        url: The full URL to audit (e.g. 'https://example.co.za')
        api_key: Google API key. Falls back to settings if not provided.

    Returns:
        PageSpeedScores dataclass with all 8 scores populated (or None on failure).
    """
    if not api_key:
        api_key = get_settings().google_places_api_key

    if not api_key:
        scores = PageSpeedScores(url=url)
        scores.had_failure = True
        scores.mobile_error = "No API key configured"
        scores.desktop_error = "No API key configured"
        return scores

    # Ensure URL has a scheme
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    scores = PageSpeedScores(url=url)

    # Fire mobile + desktop in parallel — they're independent HTTP calls.
    # Max 2 threads: one per strategy. Each may block up to 60s on slow sites.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_fetch_strategy, url, "mobile", api_key): "mobile",
            pool.submit(_fetch_strategy, url, "desktop", api_key): "desktop",
        }
        results: dict[str, dict | None] = {}
        for future in as_completed(futures):
            strategy = futures[future]
            try:
                results[strategy] = future.result()
            except Exception as exc:
                log.warning("pagespeed_future_error", url=url, strategy=strategy, error=str(exc))
                results[strategy] = None

    mobile_data = results.get("mobile")
    desktop_data = results.get("desktop")

    if mobile_data:
        scores.mobile_performance    = _extract_score(mobile_data, "performance")
        scores.mobile_seo            = _extract_score(mobile_data, "seo")
        scores.mobile_accessibility  = _extract_score(mobile_data, "accessibility")
        scores.mobile_best_practices = _extract_score(mobile_data, "best-practices")
        log.info(
            "pagespeed_mobile_done",
            performance=scores.mobile_performance,
            seo=scores.mobile_seo,
            url=url,
        )
    else:
        scores.mobile_error = "fetch failed"
        scores.had_failure = True

    if desktop_data:
        scores.desktop_performance    = _extract_score(desktop_data, "performance")
        scores.desktop_seo            = _extract_score(desktop_data, "seo")
        scores.desktop_accessibility  = _extract_score(desktop_data, "accessibility")
        scores.desktop_best_practices = _extract_score(desktop_data, "best-practices")
        log.info(
            "pagespeed_desktop_done",
            performance=scores.desktop_performance,
            seo=scores.desktop_seo,
            url=url,
        )
    else:
        scores.desktop_error = "fetch failed"
        scores.had_failure = True

    return scores
