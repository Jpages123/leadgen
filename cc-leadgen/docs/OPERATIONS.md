# cc-leadgen — Operational Runbook

> Local operations notes for the `cc-leadgen` project on the home laptop.
> Strategic / phase planning lives in `~/installedApps/cc-web-revamp/docs/WEB_REVAMP_ENGINE.md`.

## Service Topology

| Container | Role | Depends on |
|-----------|------|-----------|
| `cc-leadgen-postgres-1` | Primary DB (host: `postgres`, port 5432) | — |
| `cc-leadgen-redis-1` | Celery broker + result backend + beat schedule | — |
| `cc-leadgen-worker-1` | Executes tasks (4 concurrency) | postgres, redis |
| `cc-leadgen-beat-1` | Schedules tasks (singleton — single instance only) | redis |

Source is bind-mounted (`.:/app`) into worker + beat — file changes apply on next process start.

## Common Commands

```bash
# SSH to the laptop
ssh -i ~/.ssh/dev_access this0ne@100.89.136.18
cd ~/installedApps/leadgen/cc-leadgen

# Activate venv (use the inner one, not ~/installedApps/leadgen/.venv)
.venv/bin/python ...

# Apply Alembic migrations
docker compose run --rm worker alembic upgrade head

# Run a worker task manually (e.g. one-lead audit)
docker exec cc-leadgen-worker-1 bash -c \
  'cd /app && PYTHONPATH=/app .venv/bin/python -c "from app.workers.web_audit import run_web_audit_batch; run_web_audit_batch.delay(batch_size=5)"'

# Reset one lead for re-audit
docker exec cc-leadgen-postgres-1 psql -U postgres -d leadgen -c \
  "UPDATE leads SET web_audit_generated_at = NULL, audit_error = NULL WHERE id = '<uuid>'"

# Drain a Celery queue
docker exec cc-leadgen-redis-1 redis-cli DEL <queue_name>

# Inspect live queue
docker exec cc-leadgen-redis-1 redis-cli LLEN celery
```

## Scheduled Tasks (Celery Beat)

See `app/workers/celery_app.py` for the canonical schedule. Summary:

| Task | Cadence | Queue |
|------|---------|-------|
| `discovery-daily` | 8:00 daily | discovery |
| `enrichment-process` | every 15 min | enrichment |
| `web-audit-batch` | every 30 min | enrichment |
| `outreach-queue-leads` | every 2 hrs | outreach |
| `outreach-send` | every 30 min | outreach |
| `response-poll` (`check_replies`) | every 15 min | response |
| `mockup-approval-sync` | every 5 min | enrichment |

**Important**: Beat is a singleton. Running multiple beat instances will cause duplicate dispatches.

## Failure Modes

### 1. Beat in `Restarting (X)` loop

**Symptom**: `docker ps` shows beat cycling; no scheduled tasks fire; Celery worker logs are quiet.

**Diagnosis**:
```bash
docker logs --tail 50 cc-leadgen-beat-1
# Look for ModuleNotFoundError or ImportError
```

**Common cause**: An eagerly-imported heavy dependency in a worker module that the beat image lacks. Fix by moving the import inside the function body (see `app/reports/web_audit_report.py` for the weasyprint pattern — also see "Auditing Worker Module Imports" below).

### 2. Playwright Chromium version mismatch

**Symptom**: `audit_error` contains `BrowserType.launch: Executable doesn't exist at /root/.cache/ms-playwright/chromium_headless_shell-XXXX/...`

**Cause**: Python `playwright` lib version was upgraded (e.g. `pip install --upgrade playwright`) to a version expecting a newer Chromium binary. The build-time `playwright install` in the Dockerfile baked an older Chromium into the image.

**Fix** (runtime, idempotent):
```bash
docker exec cc-leadgen-worker-1 bash -c \
  'cd /app && .venv/bin/python -m playwright install chromium'
```

The Dockerfile + docker-compose.yml have been hardened against this — worker's startup command now does the install automatically before launching celery, so it self-heals on every container restart.

### 3. Pre-flight reachable but Playwright still fails

**Symptom**: `audit_error` contains `preflight: HTTP 200` followed by Playwright failure in subsequent runs, OR Playwright returns an error after a successful pre-flight.

**Likely causes**:
- PageSpeed API timeout (Google API key may be exhausted)
- Site loads in browser but has JavaScript that hangs / never reaches `domcontentloaded`
- Heavy Cloudflare / WAF challenge
- Playwright timeout (default 30s) too short for very slow sites

**Inspect**:
```bash
docker exec cc-leadgen-worker-1 tail -f /tmp/audit_loop.log | grep -E "web_audit_failed|pagespeed_request_error"
```

## Auditing Worker Module Imports (beat-safety)

The beat container imports every module listed in `celery_app.py` `include=[...]` at startup. **Any module-level heavy import breaks beat if that dep isn't in the beat image.**

Safe pattern (used in `app/reports/web_audit_report.py`):
```python
# BAD — breaks beat if weasyprint missing
from weasyprint import HTML, CSS

def generate_pdf(...):
    HTML(...).write_pdf(...)

# GOOD — lazy import inside function
def generate_pdf(...):
    from weasyprint import HTML       # only worker needs this
    HTML(...).write_pdf(...)
```

To audit all worker modules for eager heavy deps:
```bash
cd app/workers
for f in *.py; do
  echo "=== $f ==="
  grep -nE '^import |^from ' $f | grep -vE 'from __future__|datetime|pathlib|^from typing|^import os|^import re|^from dataclasses|^import time|^from app\.|from sqlalchemy'
done
```

## Lead Status Lifecycle

```
                  audit fails / unreachable
discovered ──────────────────────────────► invalid
    │                                          ▲
    │ score +50                                 │
    ├─► outreach_queued (sends email)          │ audit_no_score / preflight fail
    │                                          │
    ▼                                          │
 enriched ◄──── (medium/low score)              │
                                                │
 outreach_queued (139 leads)                     │
    │                                          │
    ▼                                          │
 (sent / replied / unsubscribed)               invalid (100 leads, post-2026-07-02)
```

`audit_error` column records WHY a lead was invalidated or why an audit produced score=0.

## DB Migrations

Alembic is used for `app/models` schema. Two ad-hoc SQL migrations live in `migrations/` (e.g. `2026-06-28_admin_crm_*`). For idempotent ad-hoc column additions:

```sql
ALTER TABLE leads ADD COLUMN IF NOT EXISTS audit_error TEXT;
```

Apply via:
```bash
docker exec cc-leadgen-postgres-1 psql -U postgres -d leadgen -c "..."
```

## Useful DB Queries

```sql
-- Audit completion
SELECT 
  count(*) FILTER (WHERE website IS NOT NULL) AS total_with_site,
  count(web_audit_generated_at) AS audited,
  count(*) FILTER (WHERE website IS NOT NULL AND web_audit_generated_at IS NULL) AS remaining
FROM leads;

-- Score distribution
SELECT width_bucket(web_pitch_score, 0, 100, 10)*10 AS bucket_low, count(*) 
FROM leads WHERE web_pitch_score IS NOT NULL GROUP BY 1 ORDER BY 1;

-- Leads to investigate (audit_error IS NOT NULL)
SELECT business_name, website, audit_error 
FROM leads WHERE audit_error IS NOT NULL LIMIT 20;

-- Reset a single lead for re-audit
UPDATE leads SET web_audit_generated_at=NULL, audit_error=NULL, web_pitch_score=NULL WHERE id='<uuid>';
```

---

## 2026-07-06 — Major Update: Personalization + URL Fix + Beat Break

### What was added (Session 5)

- **Scraper asset extraction**: 8 new helper functions in `app/scrapers/web_audit.py` extract logo (smallest img near header), hero (largest img), gallery (probes /gallery, /work, /portfolio), and brand color (from logo image most-saturated pixel, falls back to CSS sampling).
- **URL quality check**: `check_url_quality()` rejects 30+ booking/social/directory platforms (Fresha, Booksy, Instagram, TikTok, Facebook, Booking.com, etc.). Wired into `audit_lead()` to set `needs_url_review=True`.
- **Mockup pipeline uses scraped data**: `mockup_generator.py` skips `needs_url_review` leads, copies scraped images to `public/images/logo.jpg` and `hero.jpg` (template's hardcoded paths), passes scraped values to `generate_brand_ts()`.
- **brand.ts schema FIXED**: Generator was writing `accentColor` but template reads `brand.accent` → all CSS rendered as `style="background-color: undefined"`. Now writes the full template schema.
- **DB columns added** (migration 004): `scraped_logo_path`, `scraped_hero_path`, `scraped_gallery_paths` (JSONB), `scraped_brand_color`, `needs_url_review`, `url_quality_issue`.

### What's broken (URGENT)

**Beat container is in restart loop.** `sh: 1: /app/.venv/bin/python: not found`. My edits to `docker-compose.yml` (trying `cd /app` and absolute path) did not fix it. Beat is NOT dispatching scheduled tasks → 1,579 queued leads are NOT being processed automatically.

**Workaround to drain the queue manually:**
```bash
ssh -i ~/.ssh/dev_access this0ne@100.89.136.18
docker exec cc-leadgen-worker-1 bash -c '
cd /app && PYTHONPATH=/app .venv/bin/python -c "
from app.workers.web_audit import run_web_audit_batch
from celery.result import AsyncResult
import time
r = run_web_audit_batch.apply_async(kwargs={\"batch_size\": 50})
print(\"Task ID:\", r.id)
# poll until ready
while not r.ready():
    time.sleep(30)
    r = AsyncResult(r.id)
print(\"done:\", r.get(timeout=5))
"'
```

**Proper fix (rebuild beat image):**
```bash
cd /home/this0ne/installedApps/leadgen/cc-leadgen
docker compose build beat --no-cache
docker compose up -d --force-recreate --no-deps beat
```

### Critical learning: Worker restart required after file edits

Python loads modules at process start. **File edits to `.py` files do NOT take effect until the worker container is recreated.** Bind-mounted source files are visible to the container, but the running Python process has the OLD compiled bytecode in memory.

**Always `docker compose up -d --force-recreate --no-deps worker`** after editing code, not just `docker compose restart worker` (which preserves the original CMD and re-uses cached layers).

### Current state (2026-07-06 ~00:57 SAST)

| Metric | Value |
|--------|------:|
| Total leads with website | 1,756 |
| Audited (any time) | 1,115 |
| Has scraped brand_color | 11 |
| Has scraped logo | 8 |
| Has scraped hero | 9 |
| Has scraped gallery | 2 |
| **Queued for re-audit (not processing)** | **1,570** |
| flagged needs_url_review | 1 |
| status=invalid (skipped) | 175 |
| Worker concurrency | 8 |
| web-audit-batch batch_size | 50 |
| Beat container | ❌ **in restart loop** |
