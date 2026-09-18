"""Test Pexels API access for event-themed photos."""
import json
import os
import urllib.request
import urllib.parse

key = os.environ.get("PEXELS_API_KEY", "")
if not key:
    print("ERROR: PEXELS_API_KEY not set")
    exit(1)

queries = ["wedding marquee tent", "event dance floor", "stage lighting", "table setting elegant", "corporate gala", "wedding reception decor", "marquee letters"]
for q in queries:
    url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(q)}&per_page=2&orientation=landscape"
    req = urllib.request.Request(url, headers={"Authorization": key})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    print(f"\n=== '{q}' ({data.get('total_results', 0)} results) ===")
    for p in data.get("photos", [])[:2]:
        print(f"  src={p['src']['large']}")
        print(f"  photographer={p['photographer']} | alt={p['alt'][:80]}")
