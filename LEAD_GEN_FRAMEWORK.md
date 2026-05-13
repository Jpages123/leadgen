# Client Compass — Lead Generation Framework

> **Status**: Pipeline scaffold done · Local dashboard built · Phases 1–4 ahead  
> **Last updated**: 2026-05-13  
> **Owner**: this0ne  
> **Goal**: Build a cold outbound lead generation system on a home laptop server to find, enrich, score, and contact South African small business owners — converting them to Client Compass paying tenants.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Tech Stack](#3-tech-stack)
4. [Data Model](#4-data-model)
5. [Target Verticals & ICP](#5-target-verticals--icp)
6. [Phase 0 — Foundation](#phase-0--foundation)
7. [Phase 1 — Lead Discovery](#phase-1--lead-discovery)
8. [Phase 2 — Enrichment & Scoring](#phase-2--enrichment--scoring)
9. [Phase 3 — Outreach Engine](#phase-3--outreach-engine)
10. [Phase 4 — Response Handling](#phase-4--response-handling)
11. [Phase 5 — CRM Sync](#phase-5--crm-sync)
12. [Phase 6 — Analytics & Optimisation](#phase-6--analytics--optimisation)
13. [Local Dashboard](#local-dashboard)
14. [Compliance (POPIA)](#compliance-popia)
15. [Risks & Mitigations](#risks--mitigations)
16. [Success Metrics](#success-metrics)

---

## 1. Overview

Client Compass has no tenants yet. This system is a standalone outbound engine running on the home laptop, completely separate from the production app. It will:

1. **Discover** SA small businesses from public sources
2. **Enrich** each lead (contact info, social presence, WhatsApp signal)
3. **Score** leads and filter low-quality ones out
4. **Contact** them with personalised, vertical-specific messaging
5. **Respond** to replies intelligently and push warm leads into the main app CRM
6. **Learn** which channels/verticals convert and double down

The product sells itself here: we are selling a WhatsApp automation platform — reaching out via WhatsApp to demonstrate it is the strongest possible demo.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│               HOME LAPTOP  (Tailscale IP: 100.89.136.18)              │
│                                                                      │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────────────┐  │
│  │  Discovery   │→  │  Enrichment  │→  │    Scoring Engine      │  │
│  │  Workers     │   │  Workers     │   │                        │  │
│  └──────────────┘   └──────────────┘   └───────────┬────────────┘  │
│                                                     │               │
│  ┌───────────────────────────────────────────┐     │               │
│  │            Outreach Engine                │←────┘               │
│  │   Email (SMTP)                             │                     │
│  └──────────────────┬────────────────────────┘                     │
│                     │                                               │
│  ┌──────────────────▼──────────────────────────────────────────┐  │
│  │           PostgreSQL DB (Docker, local)                    │  │
│  │   leads │ sequences │ templates │ events │ discovery_jobs   │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │   Local Dashboard (FastAPI + Jinja2 + HTMX)                   │ │
│  │   http://localhost:8000                                        │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │  Response Handler (IMAP polling)                               │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │  Celery Beat (scheduled jobs) │ Celery Workers                │ │
│  └───────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
         │  Optional — Tailscale tunnel
         ▼
┌──────────────────────────────────────────────────────────────────────┐
│              VPS / PRODUCTION SERVER  (Tailscale: 100.82.23.104)   │
│                                                                      │
│  Nginx  →  /webhook/leadgen/whatsapp proxy_pass → laptop:8000       │
│  PostgreSQL:5432  ←  direct INSERT on admin_crm.leads (Phase 5)    │
│                                                                      │
│  admin_crm.leads (main CRM) — warm leads appear here automatically  │
└──────────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

- **Home laptop as server** — zero hosting cost, Tailscale handles private networking
- **Local dashboard** — runs on the laptop alongside the pipeline. No production VPS dependency for admin access. Can be accessed over LAN or Tailscale.
- **Tailscale for all inter-server communication** — encrypted, no firewall rules needed, works behind home NAT
- **WhatsApp webhooks via Nginx proxy on production** — production server has a public URL + SSL already. Nginx on production forwards `/webhook/leadgen/whatsapp` to the laptop's Tailscale IP. Zero extra tooling.
- **CRM sync via direct PostgreSQL over Tailscale** — laptop connects directly to `prod-tailscale-ip:5432` with a restricted `leadgen_user` role. No new API endpoint needed on production. Deferred pending VPS access.
- **IMAP polling and SMTP sending are outbound** — no public exposure needed, work fine through home internet
- **Celery + Redis** — async job queue for all workers
- **Pi/Discord integration** — operator alerts for replies, errors, daily digest

---

## 3. Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.12 | Superior scraping ecosystem (Playwright, BS4, googlemaps SDK) |
| API | FastAPI | Async, thin, familiar to Python stack |
| UI Templates | Jinja2 (via FastAPI) | Server-rendered HTML pages |
| UI Interactivity | HTMX | Partial page updates without a JS framework |
| Dashboard Charts | Text-based bars | No JS charting library, works offline |
| Queue | Celery + Redis | Battle-tested, supports scheduled + async tasks |
| Scheduler | Celery Beat | Cron-like scheduling for daily discovery jobs |
| Database | PostgreSQL 16 | Same as main app, easy reasoning |
| Migrations | Alembic | Standard for Python/Postgres |
| Scraping | Playwright + BeautifulSoup4 | JS-rendered pages + static HTML |
| Email outreach | SMTP — `outreach@go.clientcompass.co.za` | Subdomain isolates sender reputation from main domain |
| WhatsApp outreach | ⏸️ Deferred — Meta Cloud API ~R720/mo not viable pre-revenue | Enable once 4+ tenants are paying |
| Logging | structlog (JSON) | Consistent with main app |
| Containerisation | Docker Compose | Same pattern as main app |
| Ops | Makefile | Consistent developer experience |

---

## 4. Data Model

### `leads`
```sql
id                UUID        PRIMARY KEY DEFAULT gen_random_uuid()
source            VARCHAR     NOT NULL  -- 'google_maps', 'yellsa', 'cylex', 'instagram', 'manual'
source_url        TEXT
business_name     VARCHAR     NOT NULL
owner_name        VARCHAR
phone             VARCHAR     -- E.164 e.g. +27821234567
whatsapp_number   VARCHAR     -- may differ from phone
email             VARCHAR
website           TEXT
instagram_url     TEXT
facebook_url      TEXT
city              VARCHAR
province          VARCHAR
business_type     VARCHAR     -- matches Client Compass verticals
google_rating     NUMERIC(2,1)
google_review_count INT
score             INT         DEFAULT 0  -- 0-100, see scoring algo
status            VARCHAR     NOT NULL DEFAULT 'discovered'
                              -- discovered → enriched → outreach_queued
                              -- → contacted → responded → interested
                              -- → converted | not_interested | opted_out | invalid
outreach_channel  VARCHAR     -- 'email', 'whatsapp', 'both'
synced_to_crm     BOOLEAN     DEFAULT FALSE
crm_lead_id       UUID        -- admin_crm.leads.id in main app
notes             TEXT
discovered_at     TIMESTAMPTZ DEFAULT now()
last_contacted_at TIMESTAMPTZ
next_follow_up_at TIMESTAMPTZ
created_at        TIMESTAMPTZ DEFAULT now()
updated_at        TIMESTAMPTZ DEFAULT now()
```

### `outreach_sequences`
```sql
id             UUID        PRIMARY KEY DEFAULT gen_random_uuid()
lead_id        UUID        REFERENCES leads(id)
channel        VARCHAR     -- 'email', 'whatsapp'
sequence_name  VARCHAR     -- e.g. 'hair_beauty_cold_v1'
step_number    INT         -- 1, 2, 3
scheduled_at   TIMESTAMPTZ
sent_at        TIMESTAMPTZ
status         VARCHAR     -- 'pending', 'sent', 'failed', 'bounced', 'replied', 'skipped'
subject        TEXT        -- email only
message_body   TEXT
error_message  TEXT
message_id     VARCHAR     -- SMTP Message-ID for reply matching
unsubscribed   BOOLEAN    DEFAULT FALSE
created_at     TIMESTAMPTZ DEFAULT now()
```

### `outreach_templates`
```sql
id             UUID        PRIMARY KEY DEFAULT gen_random_uuid()
name           VARCHAR     UNIQUE NOT NULL
channel        VARCHAR
vertical       VARCHAR     -- NULL = all verticals
step           INT
subject        TEXT        -- email only
body           TEXT
active         BOOLEAN     DEFAULT TRUE
version        INT         DEFAULT 1  -- for A/B tracking
ab_variant     VARCHAR     -- 'A' or 'B'
created_at     TIMESTAMPTZ DEFAULT now()
```

### `lead_events`
```sql
id         UUID        PRIMARY KEY DEFAULT gen_random_uuid()
lead_id    UUID        REFERENCES leads(id)
event_type VARCHAR     -- 'email_sent', 'email_opened', 'replied', 'opted_out', 'status_change', etc.
payload    JSONB
created_at TIMESTAMPTZ DEFAULT now()
```

### `discovery_jobs`
```sql
id              UUID        PRIMARY KEY DEFAULT gen_random_uuid()
source          VARCHAR
query           VARCHAR     -- e.g. 'hair salon Cape Town'
status          VARCHAR     -- 'running', 'done', 'failed'
leads_found     INT
leads_new       INT         -- deduplicated
started_at      TIMESTAMPTZ
completed_at    TIMESTAMPTZ
error_message   TEXT
created_at      TIMESTAMPTZ DEFAULT now()
```

---

## 5. Target Verticals & ICP

### Ideal Customer Profile
- **Location**: South Africa (any city, focus on Cape Town, Johannesburg, Durban first)
- **Business size**: Sole trader to ~10 employees
- **WhatsApp signal**: Uses WhatsApp as primary communication channel (visible on Google listing or website)
- **Pain point**: Losing enquiries because they can't respond fast enough; no system for follow-ups

### Verticals — Priority Order

| Priority | Vertical | Why |
|----------|----------|-----|
| 1 | Hair & Beauty | WhatsApp-first booking culture, very common, high Google Maps density |
| 2 | Cleaning Services | Solo operators, WhatsApp-dependent, easy to find |
| 3 | Photography & Videography | High enquiry volume, strong WhatsApp presence |
| 4 | Event Planning | Coordination-heavy, WhatsApp is backbone |
| 5 | Construction & Trades | Slower but large volume of businesses |
| 6 | Automotive | Strong WhatsApp culture for quotes/bookings |
| 7 | IT & Tech Services | Smaller pool but higher deal value |
| 8 | Consulting | Longer sales cycle, lower volume |

---

## Phase 0 — Foundation

**Goal**: Provision server, project skeleton, DB, queue, logging, Discord alerts.  
**Estimated time**: 3–5 days

### ✅ Completed (2026-05-07)
- [x] OS confirmed: Debian 13
- [x] Docker Compose installed and running
- [x] Project repo created: `github.com/Jpages123/cc-leadgen`
- [x] Docker Compose services: `postgres`, `redis`, `app`, `worker`, `beat`
- [x] Project structure (see `cc-leadgen/` directory)
- [x] Alembic migrations applied (`001_initial` — all 5 tables)
- [x] `.env` configured — all variables set
- [x] `structlog` JSON logging wired throughout
- [x] FastAPI app with `/health`, `/metrics`, `/webhook/*` routes
- [x] Celery + Beat wired up with placeholder tasks per phase
- [x] Makefile: `make up`, `make down`, `make migrate`, `make logs`

### ⏸️ Pending (VPS access required)
- [ ] Add Nginx proxy block on hub (100.82.23.104): `/webhook/leadgen/` → laptop Tailscale IP
- [ ] Create `leadgen_user` on hub postgres (INSERT + SELECT on `admin_crm.leads`)
- [ ] Verify hub postgres port 5432 bound to `0.0.0.0` (not just localhost)
- [ ] Configure `go.clientcompass.co.za` subdomain DNS (SPF, DKIM, DMARC)

### ⏸️ Pending (API keys)
- [ ] Get `WABA_ID`, `PHONE_NUMBER_ID`, `SYSTEM_USER_TOKEN` from Meta Business Manager
- [ ] Add Google Cloud project + enable Places API + set $50/month budget cap

---

## Phase 1 — Lead Discovery

**Goal**: Discover 500+ raw leads across top 3 verticals.  
**Estimated time**: 1 week

### Tasks

- [ ] **Google Maps Places API** integration
  - `googlemaps` Python SDK
  - Search by `(business type) + (city)` combinations
  - Extract: `name`, `phone`, `website`, `rating`, `user_ratings_total`, `place_id`
  - Respect API rate limits (2 req/sec, cap at 1000 req/day)
- [ ] **yellsa.com scraper** (Playwright, JS-rendered)
  - Search by category + province
  - Extract same fields + email if listed
- [ ] **cylex.co.za scraper** (BeautifulSoup, mostly static)
- [ ] Deduplication: normalise phone numbers to E.164, match on `(normalised_phone OR business_name + city)`
- [ ] Store all new leads with `status = 'discovered'`
- [ ] `discovery_jobs` table tracking (start/end/counts)
- [ ] Celery Beat job: daily discovery sweep per vertical per city
- [ ] Pi/Discord alert on job completion

### City × Vertical Matrix (first run)
```python
CITIES = ["Cape Town", "Johannesburg", "Durban", "Pretoria", "Port Elizabeth"]
VERTICALS = ["hair salon", "nail salon", "cleaning services", "photographer", "event planner"]
```

---

## Phase 2 — Enrichment & Scoring

**Goal**: Add contact info to each lead, score them, queue the top 60%+ for outreach.  
**Estimated time**: 1 week

### Enrichment Tasks

- [ ] **Website crawler** (BeautifulSoup + Playwright fallback)
  - Extract: email addresses (regex), WhatsApp click-to-chat links (`wa.me`), social links
  - Detect if they already use a WhatsApp tool (Wati, Interakt, respond.io branding) → mark as `competitor_customer`
- [ ] **Email validation**: MX record check + syntax check
- [ ] **Phone normalisation**: all to E.164 `+27XXXXXXXXX`
- [ ] Update lead record with all enriched fields + set `status = 'enriched'`

### Scoring Algorithm

```python
def score_lead(lead) -> int:
    score = 0
    if lead.whatsapp_number:               score += 30
    if lead.email:                         score += 15
    if lead.phone:                         score += 10
    if lead.website:                       score += 15
    if lead.google_rating >= 4.5:          score += 10
    if lead.google_review_count >= 20:     score += 10
    if lead.instagram_url:                 score += 5
    if lead.competitor_customer:            score -= 50
    if not lead.phone and not lead.email:  score -= 20
    return max(0, min(score, 100))
```

| Score | Action |
|-------|--------|
| 60–100 | High priority → `outreach_queued` immediately |
| 40–59 | Medium priority → `outreach_queued` (second batch) |
| 20–39 | Low priority → hold, re-enrich in 30 days |
| 0–19 | Discard → `status = 'invalid'` |

---

## Phase 3 — Outreach Engine

**Goal**: Contact qualified leads with personalised, vertical-specific messages via email.  
**Estimated time**: 1.5 weeks

### ⏸️ WhatsApp — Deferred
Meta Cloud API costs ~R720/month. Enable once 4+ tenants are paying. Setup is already documented in this framework.

### Email Sequences

3-touch sequence per vertical, spaced 3–4 days apart. Stop sequence on any reply or opt-out.

**Sample Touch 1 — Hair & Beauty:**
```
Subject: Quick question about your WhatsApp bookings, [Business Name]

Hi [Owner Name / there],

Saw [Business Name] on Google Maps — looks like you're doing great work.

Quick question: do clients book or enquire through WhatsApp? If so, how are you managing it when you're with a client?

Most salon owners I talk to say WhatsApp is their busiest channel but also the hardest to keep up with.

We built a tool specifically for that — I can show you in 10 minutes if you're curious.

Worth a quick look?

[Your name]
Client Compass
https://clientcompass.co.za
```

- [ ] SMTP sender using `outreach@go.clientcompass.co.za`
- [ ] Unsubscribe link in every email footer
- [ ] Celery Beat: pick up pending sequences every hour
- [ ] Per-lead rate limiting: max 3 emails, min 3 days between touches
- [ ] Global rate cap: 50 emails/day

---

## Phase 4 — Response Handling

**Goal**: Detect replies, classify intent, alert operator.  
**Estimated time**: 1 week

- [ ] **IMAP polling** every 15 minutes on outreach inbox
- [ ] Match `In-Reply-To` / `References` headers to `outreach_sequences.message_id`
- [ ] On reply: update sequence + lead status, cancel remaining touches, alert via Discord
- [ ] Intent classification: keyword-based (positive / negative / neutral)
- [ ] `interested` → push to CRM (Phase 5)
- [ ] `neutral` → Discord alert with full reply for manual review

---

## Phase 5 — CRM Sync

**Goal**: Push warm leads into `admin_crm.leads` on the production server.  
**Estimated time**: 1–2 days

Deferred pending VPS access. Requires:
- `leadgen_user` created on hub postgres with INSERT + SELECT on `admin_crm.leads`
- `PROD_DB_URL` pointing to hub's postgres over Tailscale
- `sync_lead_to_crm(lead_id)` function with retry logic

---

## Phase 6 — Analytics & Optimisation

**Goal**: Understand what's working, double down on it.  
**Estimated time**: Ongoing

See **Local Dashboard** section below — dashboard pages replace the standalone analytics server.

| Metric | Target (Month 1) |
|--------|-----------------|
| Leads discovered / week | 200+ |
| Leads enriched (score ≥ 40) | 40%+ of discovered |
| Emails sent / week | 50 |
| Reply rate | > 5% |
| Interested → signup | > 10% |
| Cost per trial signup | < R200 |

---

## Local Dashboard

> **Running at**: `http://localhost:8000`  
> **Stack**: FastAPI + Jinja2 + HTMX (server-rendered HTML, partial swaps)  
> **Repo**: `github.com/Jpages123/cc-leadgen`

The dashboard is the local admin UI for the lead generation pipeline. It runs on the same laptop as the pipeline workers and connects to the same PostgreSQL instance. No production VPS dependency.

### Dashboard Pages

| Route | Page | Purpose |
|-------|------|---------|
| `/` | **Dashboard** | Funnel overview + text-based charts |
| `/leads` | **Leads** | Filterable/searchable table, 50/page |
| `/sequences` | **Sequences** | Outreach sequence status |
| `/discovery` | **Discovery** | Job history + manual trigger |

### Dashboard Implementation Phases

#### Phase 1 — Foundation ✅ Done (2026-05-13)
- [x] Base HTML template with top navbar (Dashboard · Leads · Sequences · Discovery)
- [x] CSS variables, system fonts, slate/zinc palette, no external CSS framework
- [x] Dashboard home: 6 metrics cards + status mix bar chart + 7-day discovery trend
- [x] Leads list: 50/pagination table, status + source dropdowns, search input
- [x] Sequences page: paginated list with status + channel filters
- [x] Discovery page: job history table + "Run Now" manual trigger
- [x] All partial templates in `templates/partials/`
- [x] `cc-leadgen/DASHBOARD.md` tracks full dashboard task list

#### Phase 2 — HTMX Interactivity 🚧 Next Session
- [ ] Add HTMX CDN + extensions to `base.html`
- [ ] Leads: status/source filters → `hx-get="/leads" hx-target="#lead-table-body" hx-swap="innerHTML"`
- [ ] Leads: search input with 400ms debounce → HTMX partial swap
- [ ] Leads: prev/next pagination → HTMX partial swap
- [ ] Sequences: status + channel filters → HTMX partial swap
- [ ] Sequences: pagination → HTMX partial swap
- [ ] Discovery: "Run Now" → `hx-post="/discovery/run"` with inline spinner + result
- [ ] Dashboard: "Refresh" → `hx-get="/metrics/partial"` into `#metrics-cards`
- [ ] New partial routes: `GET /leads/partial`, `GET /sequences/partial`
- [ ] Remove vanilla JS from discovery page, replace with HTMX equivalents

#### Phase 3 — Charts & Analytics
- [ ] Outreach pulse bar chart (emails sent per day, 7-day)
- [ ] Discovery trend text-based bar chart (7-day window)
- [ ] Lead funnel vertical chart on dashboard

#### Phase 4 — Outreach Engine UI
- [ ] Show active email sequences per lead
- [ ] Preview template rendering with real data
- [ ] Manual send override for individual leads

---

## Compliance (POPIA)

South Africa's POPIA governs processing of personal information. Cold B2B outreach is generally permitted under the **legitimate interest** basis, with conditions:

- [ ] Every email includes a one-click unsubscribe link
- [ ] Unsubscribes are honoured immediately (status → `opted_out`, no further contact)
- [ ] WhatsApp messages include opt-out instruction ("Reply STOP to opt out")
- [ ] Maximum 3 contact attempts per lead before marking `not_interested`
- [ ] No processing of personal data beyond what's needed for outreach
- [ ] Data stored only on the lead gen server (not sold or shared)
- [ ] POPIA Information Officer: clientcompass2@gmail.com
- [ ] `/webhook/unsubscribe?token=<jwt>` endpoint (already in `app/api/webhooks.py`)

---

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Google Maps API cost overrun | Medium | Low | Hard cap at $50/month in Google Cloud |
| Email sender reputation damaged | Medium | High | Daily volume cap (50/day), SPF/DKIM/DMARC, warm up slowly |
| WhatsApp quality rating drops | Medium | High | Deferred — not active until revenue covers Meta API cost |
| Low data quality from directories | High | Medium | Enrichment + scoring filters bad data before outreach |
| POPIA complaint | Low | High | Opt-out in every message, honour immediately |
| VPS access not available for CRM sync | High | Medium | Local dashboard provides full visibility; CRM sync deferred |

---

## Success Metrics

### Month 1 targets
- 1000+ leads discovered
- 400+ leads enriched and scored ≥ 40
- 200+ outreach emails sent
- 10+ replies
- 3+ trial signups

### North Star Metric
**Contacted → Trial conversion rate** (target: 5%+ by Month 2)

---

## Decisions Log

| # | Decision | Choice | Rationale |
|---|----------|--------|----------|
| 1 | WhatsApp outreach | `+27740940550` via Meta Cloud API direct | WABA already registered |
| 2 | Language | Python 3.12 | Best scraping ecosystem |
| 3 | Server | Home laptop | Zero cost, Tailscale handles networking |
| 4 | Email domain | `outreach@go.clientcompass.co.za` | Subdomain isolates cold outreach reputation |
| 5 | Channel strategy | Email-only in Phase 3 | WhatsApp deferred ~R720/mo |
| 6 | CRM sync | Direct PostgreSQL over Tailscale | No new API needed on production |
| 7 | WhatsApp webhooks | Nginx proxy on production → laptop | Reuses existing infrastructure |
| 8 | Dashboard approach | Local FastAPI + Jinja2 + HTMX | No production VPS dependency |