# Open Roles Finder

A small web app that scrapes the Greenhouse / Lever / Ashby career-page APIs of
~4,300 companies (curated by Jared Edberg during his own job search) and lets
any visitor search the cached results by keyword, location, and recency.
Built to share the tool publicly and post about it on LinkedIn.

## How it works

- **`companies_data.py`** — the curated list of ~4,300 companies (name + ATS slug).
- **`scraper.py`** — fetches every open role from each company's Greenhouse,
  Lever, or Ashby board. No keyword/location filtering happens here — every
  job gets stored, so each visitor can filter it their own way.
- **`db.py`** — a tiny SQLite layer. Jobs are upserted by URL; anything not
  seen in the last 10 scrape cycles is pruned (assumed closed/filled).
- **`boolean_search.py`** — a small AND/OR/NOT/quotes/parentheses query
  parser (e.g. `("product manager" OR "program manager") AND revenue NOT
  intern`). Evaluated in Python against each job title; SQLite is only used
  to apply the location/days/department/commitment filters and a cheap
  superset prefilter before the boolean logic runs.
- **`resume_parser.py`** — extracts text from an uploaded `.pdf`/`.docx`/`.txt`
  resume, pulls out a Skills section (if present) and likely job-title
  phrases (e.g. "Senior Product Manager"), and returns a ready-to-edit
  boolean query.
- **`app.py`** — Flask app. Runs the scraper on a schedule (every 8 hours by
  default, via APScheduler) and exposes:
  - `GET /api/jobs?q=...&location=...&days=...&department=...&commitment=...&page=...` — search
  - `GET /api/facets` — distinct department/commitment values for the filter dropdowns
  - `GET /api/status` — dataset freshness / scrape progress
  - `POST /api/refresh` — manually trigger a scrape (blocked if one's already running)
  - `POST /api/parse-resume` — multipart file upload (`resume` field), returns suggested search terms
  - `/` — the search UI (static/index.html, style.css, app.js)

A full scrape of all ~4,300 companies takes roughly 5-10 minutes with 30
parallel worker threads (tested live during development: ~98,000 jobs from
~2,300 reachable companies, after fixing a bug in the original script where
Ashby-hosted boards were silently returning zero results due to an API
schema mismatch).

### Search syntax

The search box takes a small boolean query language, not a plain keyword list:

- Bare words are implicitly ANDed: `product manager` requires both words.
- `OR` matches either side: `engineer OR designer`.
- `NOT` excludes: `salesforce NOT intern`.
- `"quoted phrases"` require the exact phrase, not just both words separately.
- `(parentheses)` for grouping: `("product manager" OR "program manager") AND revenue`.

### Facets

Department and employment-type (commitment) filters are populated from
whatever Greenhouse/Lever/Ashby actually report for each job — Greenhouse
rarely reports employment type, so that facet will be sparser for
Greenhouse-heavy results. Commitment values are normalized into a small set
(Full-time / Part-time / Contract / Temporary / Internship / Other) since
each ATS spells them differently.

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:8000. The first scrape kicks off automatically on
startup — the site is usable immediately but results will keep filling in
for the first several minutes.

## Deploy (Render.com, recommended)

1. Push this folder to a new GitHub repo.
2. In Render: **New > Web Service**, connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: (auto-detected from `Procfile`) or set explicitly:
   `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
5. Add a **persistent disk** (Render dashboard > Disks) mounted at, e.g.,
   `/data`, and set the environment variable `JOBS_DB_PATH=/data/jobs.db` so
   the SQLite cache survives restarts/redeploys. Without a persistent disk
   the app still works, it just re-scrapes from empty on every redeploy.
6. Optional environment variables:
   - `SCRAPE_INTERVAL_HOURS` (default `8`)
   - `SCRAPE_MAX_WORKERS` (default `30`)

Railway or Fly.io work the same way — any host that runs a long-lived Python
process (not a stateless serverless function, since the scheduler needs to
keep running) will work.

**Important:** use only 1 gunicorn worker (as set in the `Procfile`). Each
worker process would run its own copy of the APScheduler background job,
duplicating the scrape. If you need more concurrency for traffic, add
threads (`--threads`), not workers.

## Scope notes / what's not included

The original script this was adapted from also had scrapers for Jobvite,
iCIMS, Wellfound, YC's job board, RemoteOK, We Work Remotely, Himalayas, and
LinkedIn. Only the Greenhouse/Lever/Ashby path was actually wired into the
original script's `main()` loop, so that's what's ported here. The others
could be added later, but LinkedIn scraping in particular is fragile and
against LinkedIn's terms of service, so it's intentionally left out of the
public version.

## Branding

The header in `static/index.html` includes an "About" blurb and a LinkedIn
link. Edit the `<div class="about">` block there to change the framing, or
swap the LinkedIn URL.
