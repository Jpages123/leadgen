"""Download candidate event-themed photos from Pexels for visual review."""
import json
import os
import urllib.parse
import urllib.request

KEY = os.environ["PEXELS_API_KEY"]

# Event-themed queries, ordered by likely usefulness
QUERIES = [
    ("hero", "elegant wedding reception marquee lights night"),
    ("hero_alt", "wedding marquee tent outdoor evening"),
    ("about", "event planning team setup table"),
    ("gallery1", "wedding table setting elegant candles"),
    ("gallery2", "stage lighting concert event"),
    ("gallery3", "fairy lights wedding decoration"),
    ("gallery4", "corporate gala dinner event"),
    ("marquee_letters", "large light up letters marquee"),
]

os.makedirs("/tmp/pexels_candidates", exist_ok=True)

for label, q in QUERIES:
    url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(q)}&per_page=5&orientation=landscape"
    req = urllib.request.Request(url, headers={"Authorization": KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    photos = data.get("photos", [])
    print(f"\n=== {label} :: '{q}' ({data.get('total_results', 0)} results) ===")
    for i, p in enumerate(photos):
        # Download landscape medium for preview
        dl_url = p["src"].get("landscape") or p["src"].get("large")
        dest = f"/tmp/pexels_candidates/{label}_{i}.jpg"
        req2 = urllib.request.Request(dl_url)
        with urllib.request.urlopen(req2, timeout=30) as r2:
            with open(dest, "wb") as f:
                f.write(r2.read())
        print(f"  [{i}] {dest} | {p['width']}x{p['height']} | {p['photographer']} | {p['alt'][:60]}")
