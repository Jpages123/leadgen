# Client Compass — Lead Gen Dashboard

> **Goal**: Self-contained local dashboard for managing the lead generation pipeline on this machine.

## Tech Stack
- FastAPI (Jinja2 templates)
- HTMX for interactivity
- Text-based bar charts (no JS charting library)
- Top navbar navigation

---

## Pages

| Route | Page | Purpose |
|-------|------|---------|
| `/` | **Dashboard** | Funnel overview + charts |
| `/leads` | **Leads** | Filterable/searchable table, 50/page |
| `/sequences` | **Sequences** | Outreach sequence status |
| `/discovery` | **Discovery** | Job history + manual trigger |

---

## Implementation Phases

### Phase 1 — Foundation ✅ Done (2026-05-13)
- [x] Base HTML template (`templates/base.html`) with top navbar
- [x] Dashboard home page (`/`) with metrics cards + text-based bar charts
- [x] Leads list page with 50/pagination table + status/source/search filters
- [x] Sequences page with paginated list
- [x] Discovery page with job history + "Run Now" manual trigger (vanilla JS)
- [x] All partial templates in `templates/partials/`
- [x] starlette 1.0.0 compat: `TemplateResponse(request, name, context={})`
- [x] All 4 pages render correctly
- [x] Committed to github.com/Jpages123/cc-leadgen

### Phase 2 — HTMX Interactivity ✅ Done (2026-05-13)
- [x] Add HTMX CDN + extensions in `base.html`
- [x] Leads page: status/source filters → HTMX `hx-get="/leads/partial" hx-target="#lead-table-body" hx-swap="innerHTML"`
- [x] Leads page: search input with 400ms debounce → HTMX partial swap
- [x] Leads page: prev/next pagination → HTMX partial swap
- [x] Sequences page: status + channel filter → HTMX partial swap
- [x] Sequences page: pagination → HTMX partial swap
- [x] Discovery: "Run Now" → `hx-post="/discovery/run"` with spinner + result inline
- [x] Dashboard: "Refresh" → `hx-get="/metrics/partial"` into `#metrics-cards`
- [x] Remove vanilla JS from discovery page, replace with HTMX equivalents
- [x] New partial routes: `GET /leads/partial`, `GET /sequences/partial`
- [x] POST /discovery/run returns HTML (not JSON) for HTMX compatibility
- [x] Fix `strftime` error — discovery_jobs.started_at stored as ISO string, not datetime
- [x] Badge styles added for job statuses (running, done, failed, manual)
- [x] Committed to github.com/Jpages123/cc-leadgen

### Phase 3 — Charts & Analytics
- [ ] Outreach pulse bar chart (emails sent per day, 7-day)
- [ ] Discovery trend text-based line chart (7-day window)
- [ ] Lead funnel vertical text chart on dashboard

### Phase 4 — Outreach Engine
- [ ] SMTP email sender
- [ ] Unsubscribe endpoint (`/webhook/unsubscribe`)
- [ ] Sequence scheduler (Celery Beat integration)

### Phase 5 — CRM Sync
- [ ] Sync warm leads to production CRM (deferred until VPS access)

---

## Phase 2 — Detailed Tasks

### 2.1 Add HTMX to base.html
```html
<script src="https://unpkg.com/htmx.org@2.0.0/dist/htmx.min.js"></script>
```

### 2.2 Leads page HTMX
```
Filters row → hx-get="/leads" → hx-target="#lead-table-body"
Search input → hx-get="/leads" → hx-trigger="keyup changed delay:400ms"
Prev/Next → hx-get="/leads?page=N&..." → hx-target="#lead-table-body"
```

### 2.3 Sequences page HTMX
```
Status select → hx-get="/sequences" → hx-target="#seq-table-body"
Channel select → hx-get="/sequences" → hx-target="#seq-table-body"
Prev/Next → hx-get="/sequences?page=N&..." → hx-target="#seq-table-body"
```

### 2.4 Discovery page HTMX
```
Run Now button → hx-post="/discovery/run" → hx-target="#job-status" → hx-swap="innerHTML"
On submit: show spinner → on response: show result + refresh job list
```

### 2.5 Dashboard refresh HTMX
```
Refresh button → hx-get="/metrics/partial" → hx-target="#metrics-cards"
```

### 2.6 New partial routes needed
- `GET /leads/partial` — returns just the table body + pagination row
- `GET /sequences/partial` — returns just the table body + pagination row
- `GET /discovery/run` → keep existing POST, but return HTML result for HTMX

---

## Design Decisions

| Decision | Choice |
|----------|--------|
| Charts | Text-based bars (no JS library) |
| Pagination | 50/page server-side |
| HTMX debounce | 400ms on search inputs |
| Nav | Top horizontal navbar, fixed |
| Colors | CSS variables, slate/zinc palette |
| Font | System font stack |