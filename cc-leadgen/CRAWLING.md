# Crawling Research Log

> **Goal**: Find scrapable SA business directories for lead discovery (Phase 1).  
> **Date started**: 2026-05-13  
> **Owner**: this0ne

---

## Tested Directories

| URL | Status | Business Listings? | Notes |
|-----|--------|-------------------|-------|
| `yellsa.com` | ❌ Dead end | ❌ No | Redirects to Cameroon (`/fr`). No SA data. |
| `yellsa.co.za` | ❌ | ❌ | DNS doesn't resolve. |
| `cylex.co.za` | ❌ 403 | ❌ | Cloudflare blocked. |
| `cylex.net.za` | ❌ 403 | ❌ | Cloudflare blocked. |
| `sa-businessdirectory.co.za` | ⚠️ 200 | ❌ | Page loads, but listings are JS-rendered. Search form has `action=/all-business-listings/` but all URL patterns return 404. |
| `gumtree.co.za` | ❌ 403 | ❌ | Blocked by Cloudflare. |
| `yellowpages.co.za` | ↪️ Redirect | ❌ | 301 → `mall.yep.co.za/home`. Now just a redirect. |
| `brabys.co.za` | ❌ Unreachable | ❌ | DNS fails. |
| `ananzi.co.za` | ❌ Dead | ❌ | Redirects to dead page. |
| `yellopages.co.za` | ❌ DNS | ❌ | Doesn't exist. |
| `mall.yep.co.za` | ⚠️ 200 | ❌ | **NOTE: User reports manual search works.** API exists but returns product data, not business listings. Data may be loaded via internal APIs not yet discovered. |
| `sabusinessdirectory.co.za` | ⚠️ 200 | ⚠️ Partial | Homepage shows featured listings. Category pages (`/search-categories/hair-and-beauty/`) load with empty results. Search form present but search URLs return 404. |

---

## Active Investigation

### mall.yep.co.za
- **User confirmation**: Manual search for "hair stylist in Durbanville" returns results in browser.
- **API confirmed**:
  - `fm.mall.yep.co.za/serviceItem/categoriesByParentId` — POST, returns category tree (16 root cats, subcategories under `Medical/Wellness & Beauty` id=654)
  - `fm.mall.yep.co.za/serviceItem/search` — POST, works but returns **products**, not businesses
  - `cannon.mall.yep.co.za/api/page/getHomePage` — POST, requires auth
  - `cannon.mall.yep.co.za/api/page/getCategoryPage` — POST, parameter error
- **Problem**: The product search API works but `categoryId=757` (Hair/Nail/Skin) returned a razor product, not a hair salon.
- **Suspicion**: Business/stores are loaded via internal JS app (`/fe-shop/dev/js/app.js`) with different API endpoints not yet discovered.
- **Next step**: Inspect browser network tab during a manual search for the actual XHR URL that returns store/business listings.

### sa-businessdirectory.co.za
- **Problem**: Category pages load but return 0 listings.
- **Suspicion**: Listings load lazily via JS after page load.
- **Next step**: Long wait time (5s+) or intercept the JS that populates listings.

---

## Untested / To Explore

- [ ] `https://www.localstore.co.za` — DNS issues earlier, worth rechecking
- [ ] `https://www.citylocal.com/south-africa` — browser closed unexpectedly, re-test
- [ ] Manual browser scraping — use Playwright with longer waits + network interception
- [ ] Intercept XHR during mall.yep.co.za search to find the actual business listing API
- [ ] SA government business registry (if public)
- [ ] Facebook/Instagram business pages — requires login, not scrapable
- [ ] Google Places scrape (no API key) — Google actively blocks, against ToS

---

## Decision Criteria for Viable Source

A directory is viable if:
1. Returns business **name, phone, city** for SA queries
2. Supports **structured URL patterns** (e.g., `/search?category=hair&city=cape-town`) OR accessible via API
3. Doesn't block known scrapers/Cloudflare
4. Has enough listings across target verticals (hair/beauty, cleaning, photography, events)

---

## Current Recommendation

**Google Maps Places API** is the only currently viable path:
- Structured data: name, phone, website, rating, reviews, coordinates
- No blocking, no Cloudflare
- SA coverage: strong across Cape Town, Johannesburg, Durban, Pretoria
- Cost: ~$0.032/ Places API search call. $7/day at 200 searches. $200/month free credit on new account.
- Setup time: ~20 minutes at [console.cloud.google.com](https://console.cloud.google.com)

**Alternative while waiting for API key**: Build CSV import UI to test the pipeline end-to-end with manual lead entry.

---

## Actions

- [ ] **User to confirm**: Can you open mall.yep.co.za DevTools → Network tab, search "hair stylist Durbanville", and share the XHR URL that returns the actual business/store results?
- [ ] **Playwright**: Retry `sa-businessdirectory.co.za` with 5s page wait + scroll-to-trigger
- [ ] **Playwright**: Intercept all XHR on mall.yep.co.za during manual search to find real API
- [ ] **CSV import UI**: Build while waiting for Google Maps API key
---

## Final Decision (2026-06-27)

**Yellsa**: Confirmed dead for SA. The site pivoted to a Cameroon freelancer platform ( locale,  hardcoded).  returns 404.  returns "No artisans found" with Cameroon data. **Drop the scraper entirely.**

**Cylex**: Cloudflare 403. Not worth fighting. **Drop.**

**Google Places API**: The replacement for both. Confirmed viable — returns , , , ,  for SA business searches. ~$0.032/call. $200/month free credit on new Google Cloud account covers ~6,000 searches.

**Action**: Implement . Add  to . This is the current blocker for Phase B.


---

## Final Decision (2026-06-27)

**Yellsa**: Confirmed dead for SA. The site pivoted to a Cameroon freelancer platform (locale en/fr, country=cm hardcoded). The URL /search/hairdressers/cape-town returns 404. The en path with cape-town query returns 'No artisans found' with Cameroon data. Drop the scraper entirely.

**Cylex**: Cloudflare 403. Not worth fighting. Drop.

**Google Places API**: The replacement for both. Returns name, phone, website, rating, review_count for SA business searches. ~zsh.032/call. /month free credit on new Google Cloud account covers ~6,000 searches.

**Action**: Implement app/scrapers/google_places.py. Add GOOGLE_PLACES_API_KEY to .env. This is the current blocker for Phase B.
