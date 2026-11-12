#!/usr/bin/env python3
"""
Yep Mall Store Scraper — Lead Discovery Source
API: POST https://fm.mall.yep.co.za/api/seller/searchStore
API: POST https://fm.mall.yep.co.za/api/seller/detail

Rate limits: conservative, 1 req/sec max. No auth needed.
"""
import json
import time
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import asdict
from typing import Optional

import requests

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "leads.db"
API_SEARCH = "https://fm.mall.yep.co.za/api/seller/searchStore"
API_DETAIL = "https://fm.mall.yep.co.za/api/seller/detail"

HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://mall.yep.co.za/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}

# Priority verticals matching LEAD_GEN_FRAMEWORK.md target ICP
SEARCH_TERMS = [
    # Hair & Beauty (highest priority)
    "hair salon",
    "nail salon",
    "beauty salon",
    "barber",
    "hair braider",
    "eyelash extensions",
    # Cleaning Services
    "house cleaning",
    "carpet cleaning",
    "office cleaning",
    "window cleaning",
    # Photography & Videography
    "photographer",
    "event photographer",
    "wedding photographer",
    "videographer",
    # Event Planning
    "event planner",
    "party planner",
    "wedding planner",
    # Construction & Trades
    "plumber",
    "electrician",
    "painter decorator",
    "gardener",
    "landscape gardener",
    # Automotive
    "car mechanic",
    "auto repair",
    "panel beater",
    "tyres",
]

PAGINATION_SIZE = 50  # max the API supports


def init_db():
    """Create SQLite leads table."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT DEFAULT 'yep_mall',
            source_url TEXT,
            seller_id INTEGER,
            business_name TEXT NOT NULL,
            owner_name TEXT,
            phone TEXT,
            whatsapp_number TEXT,
            email TEXT,
            website TEXT,
            description TEXT,
            city TEXT,
            province TEXT,
            suburb TEXT,
            postal_code TEXT,
            country TEXT DEFAULT 'South Africa',
            latitude REAL,
            longitude REAL,
            category TEXT,
            service_range INTEGER,
            premium_seller INTEGER,
            discovered_at TEXT,
            detail_fetched INTEGER DEFAULT 0,
            UNIQUE(seller_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT,
            page_no INTEGER,
            stores_found INTEGER,
            new_stores INTEGER,
            duration_ms INTEGER,
            error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def search_stores(keyword: str, page_no: int = 1, page_size: int = 50) -> dict:
    """Call the searchStore API."""
    payload = {"keyword": keyword, "pageNo": page_no, "pageSize": page_size}
    resp = requests.post(API_SEARCH, headers=HEADERS, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_store_detail(seller_id: int) -> Optional[dict]:
    """Fetch full store detail by seller ID."""
    payload = {"sellerId": seller_id}
    resp = requests.post(API_DETAIL, headers=HEADERS, json=payload, timeout=15)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    return None


def parse_city_province(store_address: str) -> dict:
    """Parse city/province/suburb from storeAddress string."""
    if not store_address:
        return {"city": "", "province": "", "suburb": "", "postal_code": "", "country": "South Africa"}
    parts = [p.strip() for p in store_address.split(",")]
    provinces = {
        "Gauteng", "Western Cape", "Eastern Cape", "KwaZulu-Natal",
        "Northern Cape", "Free State", "Mpumalanga", "Limpopo", "North West"
    }
    city = province = suburb = postal_code = ""
    for i, p in enumerate(parts):
        p_clean = p.strip()
        if p_clean in provinces:
            province = p_clean
            if i > 0:
                prev1 = parts[i - 1].strip()
                # Skip if prev is a number (postal code slipped in)
                if not prev1.isdigit():
                    city = prev1
                    if i > 1:
                        prev2 = parts[i - 2].strip()
                        if not prev2.isdigit():
                            suburb = prev2
            if i + 1 < len(parts) and parts[i + 1].strip().isdigit():
                postal_code = parts[i + 1].strip()
            break
        if p_clean.isdigit() and len(p_clean) == 4:
            postal_code = p_clean
    return {"city": city, "province": province, "suburb": suburb,
            "postal_code": postal_code, "country": "South Africa"}


def normalize_phone(raw: str) -> Optional[str]:
    """Normalize SA phone to E.164 format.
    
    Accepts: 0821234567, +27821234567, 821234567, 113268334, etc.
    Returns: +27821234567 or None if ambiguous/invalid.
    
    9-digit numbers: '6/7/8' prefix = mobile (073, 082, 083), '1' prefix = landline (011, 012...)
    We accept mobile (9-digit starting with 6/7/8), skip landline (would need area code guessing).
    10-digit: prepend +27.
    11-digit starting with 27: accept if valid mobile prefix at pos 3.
    """
    if not raw:
        return None
    digits = "".join(filter(str.isdigit, str(raw)))
    if len(digits) == 0:
        return None

    if digits.startswith("27") and len(digits) == 11:
        # Already E.164
        return f"+{digits}" if digits[2] in "6789" else None

    if digits.startswith("0") and len(digits) == 10:
        # Standard SA: 0821234567 -> +27821234567
        return f"+27{digits[1:]}"

    if len(digits) == 9:
        if digits[0] in "6789":
            # Mobile missing 0: 821234567 -> +27821234567
            return f"+27{digits}"
        # 9-digit starting with 1 = landline (011, 012, 013...) — can't guess reliably
        return None

    return None


def extract_whatsapp(description: str, phone: str) -> Optional[str]:
    """Check description for WhatsApp links."""
    if not description:
        return None
    import re
    if "wa.me" in description or "whatsapp" in description.lower():
        # Extract from detail page if needed
        pass
    return None


def upsert_lead(conn: sqlite3.Connection, store: dict, detail: dict = None,
                keyword: str = "") -> bool:
    """
    Insert or update a lead. Returns True if new, False if already existed.
    """
    addr = parse_city_province(store.get("storeAddress", ""))
    phone_raw = store.get("primaryContactNumber", "")
    alt_raw = store.get("alternativeContactNumber", "")
    phone = normalize_phone(phone_raw) if phone_raw else None
    # If primary didn't normalize, try alt
    if not phone:
        phone = normalize_phone(alt_raw) if alt_raw else None
    # Prefer primary number, use alt as WhatsApp signal if different
    whatsapp = normalize_phone(alt_raw) if alt_raw and alt_raw != phone_raw else None
    source_url = f"https://mall.yep.co.za/store/{store.get('sellerId', '')}"

    business_name = store.get("storeName", "").strip()
    if not business_name:
        return False

    # Extract category from description if available
    category = keyword

    # Enrich from detail if available
    email = None
    website = None
    description = store.get("storeDescription", "") or ""
    discovered_at = datetime.now(timezone.utc).isoformat()

    # Check if already existed using SELECT first
    existing = conn.execute(
        "SELECT id FROM leads WHERE seller_id = ?", (store.get("sellerId"),)
    ).fetchone()
    if existing:
        # Update non-nullable fields only
        conn.execute(
            "UPDATE leads SET business_name=?, phone=COALESCE(?,phone),"
            "description=?, discovered_at=? WHERE seller_id=?",
            (business_name, phone, description, discovered_at, store.get("sellerId"))
        )
        return False

    conn.execute(
        """
        INSERT INTO leads (
            source, source_url, seller_id, business_name, phone, whatsapp_number,
            email, website, description, city, province, suburb, postal_code,
            latitude, longitude, category, service_range, premium_seller,
            discovered_at, detail_fetched
        ) VALUES (
            'yep_mall', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0
        )""",
        (source_url, store.get("sellerId"), business_name, phone, whatsapp,
         email, website, description, addr["city"], addr["province"], addr["suburb"],
         addr["postal_code"], store.get("storeAddressLatitude"),
         store.get("storeAddressLongitude"), category, store.get("serviceRange"),
         1 if store.get("premiumSeller") else 0, discovered_at)
    )
    return True


def log_scrape(conn: sqlite3.Connection, term: str, page_no: int,
               found: int, new: int, duration_ms: int, error: str = None):
    conn.execute(
        """INSERT INTO scrape_log (term, page_no, stores_found, new_stores, duration_ms, error)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (term, page_no, found, new, duration_ms, error)
    )
    conn.commit()


def scrape_keyword(conn: sqlite3.Connection, keyword: str,
                   max_pages: int = 5, delay: float = 1.0) -> dict:
    """Scrape one keyword search term across pages."""
    stats = {"total_found": 0, "new": 0, "errors": 0}

    for page_no in range(1, max_pages + 1):
        start = time.time()
        try:
            result = search_stores(keyword, page_no, PAGINATION_SIZE)
        except Exception as e:
            log_scrape(conn, keyword, page_no, 0, 0, 0, str(e))
            stats["errors"] += 1
            break

        duration_ms = int((time.time() - start) * 1000)
        data = result.get("data", {})
        stores = data.get("list", [])
        total = data.get("total", 0)

        if not stores:
            break

        new_count = 0
        for store in stores:
            # Optionally fetch detail for premium/interesting leads only
            # detail = get_store_detail(store["sellerId"]) if store.get("premiumSeller") else None
            detail = None  # skip for now to stay fast
            if upsert_lead(conn, store, detail, keyword):
                new_count += 1

        log_scrape(conn, keyword, page_no, len(stores), new_count, duration_ms)
        stats["total_found"] += len(stores)
        stats["new"] += new_count

        print(f"  [{keyword}] page {page_no}: {len(stores)} stores ({new_count} new), "
              f"total so far={stats['total_found']}/{total}")

        if len(stores) < PAGINATION_SIZE:
            break  # last page

        stats["total_found"] = total  # we've reached the end
        time.sleep(delay)

    return stats


def main():
    print("=== Yep Mall Scraper — Lead Discovery ===")
    print(f"Database: {DB_PATH}")
    conn = init_db()

    total_new = 0
    for term in SEARCH_TERMS:
        print(f"\nSearching: '{term}'")
        stats = scrape_keyword(conn, term, max_pages=3, delay=1.0)
        total_new += stats["new"]
        if stats["errors"] > 2:
            print(f"  ⚠ Too many errors ({stats['errors']}), pausing 10s...")
            time.sleep(10)

    # Summary
    cur = conn.execute("SELECT COUNT(*) FROM leads WHERE source='yep_mall'")
    total_leads = cur.fetchone()[0]
    print(f"\n=== Done ===")
    print(f"New leads added this run: {total_new}")
    print(f"Total leads in DB: {total_leads}")

    # Show sample
    print("\nSample leads:")
    rows = conn.execute(
        "SELECT business_name, phone, city, province, category "
        "FROM leads WHERE source='yep_mall' ORDER BY discovered_at DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        print(f"  {r[0]} | {r[1]} | {r[2]}, {r[3]} | {r[4]}")

    conn.close()


if __name__ == "__main__":
    main()