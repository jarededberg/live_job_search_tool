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
   - `SCRAPE_MAX_WORKERS` (default `4`)

### Memory

This went through three real rounds of fixing after hitting Render's memory
limit in production, worth understanding if you ever tune it further:

1. **Greenhouse's `?content=true` flag** was dropped from requests. That flag
   returns the full HTML job description on every posting (several MB per
   company for big boards), which we never used. It also happens to be the
   only way to get `departments` back from Greenhouse, so Greenhouse-sourced
   jobs no longer populate the department facet — Lever and Ashby jobs still
   do, since their APIs don't gate that behind an extra flag.
2. **Streaming instead of accumulating.** The scraper used to hold every job
   found (~85-100k dicts) in memory for the entire ~10-minute scrape before
   writing anything to disk. It now flushes to SQLite every `batch_size`
   (default 100) jobs and forces a GC pass after each flush, so peak memory
   no longer grows with how much of the company list has been scanned.
3. **Streaming the Lever/Ashby JSON parse itself.** Unlike Greenhouse, Lever
   and Ashby have no opt-out for full HTML+plain-text job descriptions
   (~17-19KB per job, no flag to drop them) — a single large company's raw
   API response can be several MB. Fixes #1 and #2 above didn't touch this:
   each company's full response was still being `json.loads()`'d into memory
   before being thrown away, and that's what kept crashing the app even after
   the batching fix shipped. `scraper.py` now parses Greenhouse, Lever, and
   Ashby responses with `ijson` (streaming JSON parser) via `open_stream()` /
   `stream_items()`, extracting only the ~6 fields actually used per job
   instead of materializing the whole response. Verified this doesn't change
   any extracted data (identical output vs. the old parser on several
   companies) and cut peak memory for the single worst-case company tested
   (Palantir, 308 Lever jobs, 5.8MB raw payload) from an estimated 15-20MB+
   down to under 1MB.

With all three fixes, a run of 797 of the ~4,300 companies
(`SCRAPE_MAX_WORKERS=4`, `batch_size=100`) peaked at **56MB** RSS — down from
274MB before fix #3, and comfortably under Render's 512MB Starter-tier limit.
`SCRAPE_MAX_WORKERS` and `batch_size` (in the `scrape_all()` call in
`app.py`) are still the two levers if memory ever gets tight again, but at
this point there's real headroom.

Railway or Fly.io work the same way — any host that runs a long-lived Python
process (not a stateless serverless function, since the scheduler needs to
keep running) will work.

### "database is locked" errors

If you ever see this on `/api/jobs` (typically while a scrape is running),
it's a connection leak, not a concurrency limitation of SQLite itself.
`db.py` originally used `with sqlite3.connect(...) as conn:` everywhere —
that pattern only commits/rolls back on exit, it does **not** close the
connection. `upsert_jobs()` gets called on the order of hundreds of times
per full scrape (once per 100-job batch), so every scrape leaked hundreds of
open connections holding locks on the WAL file, eventually blocking ordinary
reads. Fixed by routing every DB function through `conn_ctx()`, a context
manager that guarantees `conn.close()` on the way out. Stress-tested with 2
threads continuously writing 100-job batches against 6 threads continuously
reading, for 8 seconds straight — zero lock errors.

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
