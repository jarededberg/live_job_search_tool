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
- **`salary_extractor.py`** — best-effort salary range extraction. Ashby
  sometimes exposes a real structured compensation field (used when
  present); everything else falls back to a conservative regex over the job
  description text. See "Salary data" below for the honest accuracy story.
- **`blurb_extractor.py`** — best-effort role summary for the card view.
  Prioritizes an explicit years-of-experience requirement, falls back to
  bullets under a "Qualifications"/"Requirements" heading, falls back to the
  first substantive sentence. See "Role blurbs" below.
- **`build_logo_cache.py`** — offline script that resolves a working favicon
  domain for each company and writes `static/logo_cache.json`. Run this
  occasionally (not on every deploy) when companies_data.py changes — see
  "Company logos" below.
- **`app.py`** — Flask app. Runs the scraper on a schedule (every 8 hours by
  default, via APScheduler) and exposes:
  - `GET /api/jobs?q=...&location=...&location=...&days=...&department=...&commitment=...&sort=...&page=...` — search (repeat `location` for multi-select; `sort` is one of `newest`/`oldest`/`company`/`salary_high`/`salary_low`, default `newest`)
  - `GET /api/facets` — distinct department/commitment values for the filter dropdowns
  - `GET /api/locations?q=...` — location typeahead suggestions, ranked by role count
  - `GET /api/status` — dataset freshness / scrape progress
  - `POST /api/refresh` — manually trigger a scrape (blocked if one's already running)
  - `POST /api/parse-resume` — multipart file upload (`resume` field), returns suggested search terms
  - `/` — the search UI (static/index.html, style.css, app.js)
  - `/logo_cache.json` — served as a static file, fetched once client-side

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

### Location filter

Multi-select with type-ahead, not a plain text box. `#location-input` in
`static/app.js` debounces (200ms) and hits `GET /api/locations?q=...`, which
returns the top 20 distinct location strings (by role count) that contain
the typed substring — ranked so "Remote" and major hubs surface before
long-tail office locations. Selecting a suggestion adds a chip; multiple
chips are OR'd together server-side (`db.search_jobs(locations=[...])`).
Jobs with no reported location always pass the filter rather than getting
hidden by a filter they have no data to match against.

### Salary data

None of Greenhouse, Lever, or Ashby expose salary as a clean structured
field consistently:

- **Ashby** sometimes does — pass `?includeCompensation=true` (undocumented
  in the obvious places; found by comparing a rendered job page, which shows
  a "Compensation" section with per-location pay bands, against what the
  plain API response was missing) and some postings return a real
  `compensation.compensationTiers[].components[]` array with `minValue`/
  `maxValue`/`currencyCode`/`interval`. Used directly when present — this is
  exact data, not a guess. Only `interval == "1 YEAR"` and `currencyCode ==
  "USD"` components are used, since some postings (contract/hourly roles)
  quote an hourly rate that would otherwise get silently averaged in next to
  six-figure annual salaries.
- **Greenhouse and Lever** (and any Ashby posting without the structured
  field) fall back to `salary_extractor.py`, a conservative regex over the
  job description text: `$120,000 - $150,000`, `$120k-$150k`, "between
  $X and $Y", or a single `$130,000 per year` figure. Deliberately narrow —
  it requires a `$` sign and comma/k-formatted numbers in a sane annual-
  salary range (\$15k–\$1M), specifically to avoid catching things like
  "$50,000,000 Series B" or phone numbers. It'll miss real disclosures
  written in unusual formats; it should rarely show a wrong one.

Every salary tag in the UI is prefixed `~` and has a tooltip noting it's a
best-effort figure, not a verified one — this matters more for the regex
path than the Ashby structured path, but the UI doesn't try to distinguish
the two sources.

Measured across an 800-company sample: ~45% of scraped jobs end up with a
salary figure (department coverage is ~100% now that Greenhouse's
`content=true` is back on — see Memory section below for why that's safe).

### Company logos

Clearbit's logo API (the obvious choice) is dead. Google's favicon service
(`https://www.google.com/s2/favicons?domain=X&sz=64`) is used instead, but
it needs an actual working domain — and company names/ATS slugs in
`companies_data.py` often aren't one ("scaleai" 404s; the real domain is
`scale.com`). Guessing wrong isn't just imprecise, it can be confidently
*wrong*: naively turning "Apollo.io" into "apolloio.com" resolves to a real
but unrelated site, showing someone else's logo.

`build_logo_cache.py` handles this once, offline, for all ~3,500 unique
companies rather than guessing live on every page render:

1. Generates domain candidates per company — the ATS slug and the company
   name (each tried with `.com`/`.ai`/`.io`/`.co`), plus, when the company
   name already spells out its own TLD (e.g. "Character.AI", "Apollo.io"),
   that exact domain is tried first since it's a far stronger signal than
   any generic guess.
2. HEAD-requests Google's favicon endpoint for each candidate in order;
   Google returns a real 404 (not a generic fallback icon) when a domain
   has no resolvable favicon, so the first 200 wins.
3. Writes `static/logo_cache.json` (`{"Company Name": "domain.com" | null}`),
   served as a static file and fetched once client-side.

Resolved 3,306 of 3,453 unique company names (96%) in ~2.5 minutes with 30
concurrent workers. Companies that don't resolve get a lettered fallback
avatar in the UI (`job-logo-fallback` in style.css) instead of a broken
image icon.

Re-run `python3 build_logo_cache.py` (safe to re-run — it skips companies
already in the cache) whenever `companies_data.py` gets new entries. It's
not part of the deploy pipeline or the scheduled scrape; it's a manual,
occasional step, same as editing the company list itself.

### Role blurbs

Each card shows a short blurb — same "best-effort, clearly extracted, not
curated" philosophy as salary. `blurb_extractor.py` tries, in order:

1. An explicit years-of-experience mention ("4+ years of experience
   prospecting...", "Minimum 3 years of experience building in AWS...").
2. The first couple bullet points under a heading that looks like
   "Qualifications" / "Requirements" / "What You'll Need" / "Who You Are".
   For Lever specifically, this uses the `lists` field (already split into
   titled sections like "What We Require") instead of hunting for a heading
   in a wall of text — far more reliable, since Lever's plain-text fields
   are often just a repeated company boilerplate paragraph.
3. The first substantive sentence of the description, as a last resort.

On an 800-company sample, 100% of jobs get some blurb (the fallback ensures
that), and the years-of-experience / qualifications passes catch a large
majority of them with something genuinely specific rather than generic
company-intro text.

One real bug worth knowing about if this ever gets touched again: the
original years-of-experience regex baked "grab ~10 words before and ~14
after" directly into the pattern via bounded repetition of a
variable-length token class (`(?:\S+\s+){0,10}...`). That measured ~14ms
per call on a real ~8KB job description — 7.75s for one 517-job company,
which would have meant a full ~4,300-company scrape taking noticeably
longer than before. The fix: match only the short anchor phrase with a
plain regex, then grab surrounding context with ordinary Python string
splitting instead of asking the regex engine to do it. Same 517 jobs
dropped to ~0.4s. If you're tempted to add more context-aware regexes here,
benchmark against real data before assuming it's fine at scale — a pattern
that's instant on a test string can still be slow-by-orders-of-magnitude on
a real 8KB description.

### Sort control

Default is newest-first — but "newest" only reads as newest if postings
actually have distinct timestamps. `posted` used to be truncated to a bare
date (`parse_ts` in `scraper.py`), so the dozens of jobs posted on the same
day fell back to the secondary sort key (company name), which visually
looked like the whole list was alphabetized even though newest-first was
technically the primary sort. `parse_ts` now keeps a full ISO-8601
timestamp; the UI still only displays the date part
(`job.posted.slice(0, 10)` in `jobRow`/`jobCard`), but the underlying sort
now has real second-level precision. `SORT_OPTIONS` in `db.py` covers
newest / oldest / company A-Z / salary high-to-low / salary low-to-high;
the `#sort` dropdown applies immediately on change rather than waiting for
the Search button, since it's "how do you want to look at what you already
have," not a new query.

### Match tiers (best / good / poor)

Purely client-side, computed in the browser, not stored or scraped: once a
resume is uploaded, `app.js` keeps the extracted terms in memory and scores
each visible job by what fraction of those terms appear in its title +
blurb — ≥50% match is "Best match," ≥20% is "Good match," otherwise "Poor
match." This is a keyword-overlap heuristic, not semantic matching or a
model call — it's the same tier of signal as the salary/blurb extraction
(clearly labeled via a tooltip on the badge), not a claim about actual job
fit. No resume uploaded yet = no badges shown at all, rather than a
meaningless default tier.

### Results layout

Cards are laid out in a responsive grid — 5 across at full desktop width,
stepping down to 4 / 3 / 2 / 1 as the viewport narrows (see `.results-grid`
in `style.css`). The site's overall max-width grew from 1100px to 1360px to
give 5 columns reasonable breathing room.

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

Salary extraction (see below) later added Greenhouse's `?content=true` flag
back and Ashby's `?includeCompensation=true` flag — both add payload size
per job, which is exactly the kind of change that broke this before. Both
were re-verified the same way: `content=true` alone measured 459KB peak
traced memory for a 518-job Greenhouse board (vs. ~5.7MB of raw description
text streamed through it), and the same 797-company benchmark re-run with
both flags on peaked at **60MB** RSS — a 4MB increase, not a regression.
Streaming is what makes this safe: payload size stopped being the thing that
mattered once nothing holds more than ~1 job's data in memory at a time.

Adding blurb extraction (storing a short ~220-char excerpt per job, on top
of salary) brought the same 797-company benchmark to **85MB** RSS — still
well within budget. (This is also where a real performance bug got caught
and fixed — see "Role blurbs" below; the fix mattered for scrape *duration*
more than memory, but it's the same instinct: benchmark on real data before
assuming a regex is fine at scale.)

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
