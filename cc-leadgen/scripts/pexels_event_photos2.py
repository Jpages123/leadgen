"""Download candidate event-themed photos from Pexels for visual review (httpx)."""
import json
import os
import urllib.parse

import httpx

KEY = os.environ["PEXELS_API_KEY"]
HEADERS = {"Authorization": KEY}

QUERIES = [
    ("hero", "elegant wedding reception marquee lights night"),
    ("hero_alt", "wedding marquee tent outdoor evening"),
    ("about", "event planning team setup table"),
    ("gallery1", "wedding table setting elegant candles"),
    ("gallery2", "stage lighting concert event"),
    ("gallery3", "fairy lights wedding decoration"),
    ("gallery4", "corporate gala dinner event"),
    ("marquee_letters", "large light up letters marquee"),
    ("dance_floor", "dance floor event lighting"),
]

os.makedirs("/tmp/pexels_candidates", exist_ok=True)

with httpx.Client(timeout=30, headers=HEADERS) as client:
    for label, q in QUERIES:
        r = client.get(
            "https://api.pexels.com/v1/search",
            params={"query": q, "per_page": 5, "orientation": "landscape"},
        )
        r.raise_for_status()
        data = r.json()
        photos = data.get("photos", [])
        print(f"\n=== {label} :: '{q}' ({data.get('total_results', 0)} results) ===")
        for i, p in enumerate(photos):
            dl_url = p["src"].get("landscape") or p["src"].get("large")
            dest = f"/tmp/pexels_candidates/{label}_{i}.jpg"
            with open(dest, "wb") as f:
                f.write(client.get(dl_url).content)
            print(f"  [{i}] {dest} | {p['width']}x{p['height']} | {p['photographer']} | {p['alt'][:60]}")
