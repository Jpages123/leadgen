#!/usr/bin/env python3
"""
Import leads from SQLite (scrapers/leads.db) into the Postgres leadgen DB.
Uses the sync SQLAlchemy session — safe to run outside Docker network.
"""
import sys
from datetime import datetime, timezone

# Add cc-leadgen to path so we can import its modules
sys.path.insert(0, "/home/this0ne/installedApps/leadgen/cc-leadgen")

from app.db.sync_session import sync_session_scope
from app.models.lead import Lead

try:
    import sqlite3
except ImportError:
    import sqlite3  # fallthrough


def import_leads():
    sqlite_path = "/home/this0ne/installedApps/leadgen/scrapers/leads.db"

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    total = 0
    imported = 0
    skipped = 0

    with sync_session_scope() as session:
        # Get existing seller_ids in Postgres so we skip duplicates
        existing = set(
            r[0]
            for r in session.execute(
                "SELECT CAST(source_url AS TEXT) FROM leads WHERE source = 'yep_mall'"
            ).fetchall()
            if r[0]
        )
        # Also track by seller_id if we can parse it from source_url
        existing_sellers = set()
        for url in existing:
            if "/store/" in str(url):
                try:
                    sid = int(str(url).split("/store/")[1].split("?")[0].split("#")[0])
                    existing_sellers.add(sid)
                except (ValueError, IndexError):
                    pass

        print(f"Postgres has {len(existing_sellers)} existing Yep Mall leads")

        rows = sqlite_conn.execute(
            """
            SELECT seller_id, business_name, phone, whatsapp_number, email,
                   website, description, city, province, suburb, postal_code,
                   country, latitude, longitude, category, service_range,
                   premium_seller, source_url, discovered_at
            FROM leads
            WHERE seller_id IS NOT NULL
            ORDER BY discovered_at
            """
        ).fetchall()

        total = len(rows)
        print(f"SQLite has {total} leads to import")

        for row in rows:
            seller_id = row["seller_id"]
            if seller_id and seller_id in existing_sellers:
                skipped += 1
                continue

            try:
                discovered_at = None
                if row["discovered_at"]:
                    try:
                        discovered_at = datetime.fromisoformat(row["discovered_at"].replace("Z", "+00:00"))
                    except Exception:
                        discovered_at = datetime.now(timezone.utc)

                lead = Lead(
                    source="yep_mall",
                    source_url=row["source_url"],
                    business_name=row["business_name"] or "Unknown",
                    phone=row["phone"],
                    whatsapp_number=row["whatsapp_number"],
                    email=row["email"],
                    website=row["website"],
                    city=row["city"],
                    province=row["province"],
                    status="discovered",
                    score=0,
                    discovered_at=discovered_at,
                )
                session.add(lead)
                imported += 1

            except Exception as e:
                print(f"  Error importing {row['business_name']}: {e}")
                skipped += 1

        session.commit()

    sqlite_conn.close()

    print(f"\n=== Import Complete ===")
    print(f"Total from SQLite: {total}")
    print(f"Imported: {imported}")
    print(f"Skipped (duplicate): {skipped}")


if __name__ == "__main__":
    import_leads()