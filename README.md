# Skip The Boards

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
  - `GET /api/jobs?q=...&location=...&location=...&days=...&department=...&department=...&commitment=...&sort=...&page=...` — search (repeat `location`/`department` for multi-select; `sort` is one of `newest`/`oldest`/`company`/`salary_high`/`salary_low`, default `newest`)
  - `GET /api/facets` — distinct department/commitment values for the filter dropdowns (department values are cleaned of cohort/program tags like "EMEA '24" — see "Department cleanup" under AI Search below)
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

Beyond raw substrings, the dropdown also pins five canonical **group**
options — "Remote (US)", "Remote (Canada)", "Remote (UK)", "Remote
(Europe)", "Remote (unspecified / global)" — fetched from `GET
/api/location-groups`. These exist because Greenhouse/Lever/Ashby store
whatever free-text location string a company typed into their ATS: an
800-company sample turned up 865 distinct strings containing the word
"remote" ("Remote - US", "US Remote", "REMOTE - USA",
"California, USA, Remote", "AMER-US-Remote", ...). `location_groups.py`
classifies a raw location string into a group via regex/keyword heuristics
(the word "remote" plus a US state name/abbreviation, "canada", a UK/Europe
signal, etc.) rather than an exact-match lookup table, since there's no way
to enumerate every real-world spelling in advance. Selecting "Remote (US)"
sends `location_group=remote_us`; `db.search_jobs()` first broadens the SQL
query to any row containing "remote" when a group is requested, then
applies the precise `matches_group()` classification in Python afterward —
group and plain-text location chips are OR'd together, same as multiple
plain-text chips are. Group chips render in a darker color in both the
dropdown (pinned at the top, marked with a ★) and as selected chips, so
they're visually distinct from a raw scraped-location chip.

### Salary and YOE range filters

Two dual-thumb "min-max bar" sliders — Salary and Years of experience — sit
below the location/department/commitment row. Custom-built with plain
pointer events rather than two overlapping native `<input type="range">`
elements: overlapping range inputs have real quirks around which thumb
grabs a click when both sit at the same position, and this app already
avoids pulling in a slider library for the rest of its UI. `createRangeSlider()`
in `app.js` is the reusable factory both sliders are built from — drag via
`pointerdown`/`pointermove`/`pointerup` with `setPointerCapture`, arrow-key
nudging for keyboard users, click-on-track to jump the nearer handle, and a
live label that reads "Any" untouched, "$150k+" when only the floor moved,
"Up to $150k" when only the ceiling moved, or "$120k – $150k" once both
handles are off their endpoints.

The salary slider's own endpoints aren't hardcoded — `GET /api/facets`
returns `salary_bounds`, computed from what's actually in the dataset
(`db.salary_bounds()`) rather than an arbitrary guess, so the floor/ceiling
always reflect real postings, rounded outward to the nearest $5k for clean
slider endpoints.

**The YOE slider's bounds ARE hardcoded, deliberately — fixed at 0 to 20,
where 20 means "20+".** This one used to be data-derived the same way
salary is (fetch distinct `years_experience` strings, parse each with
`blurb_extractor.parse_years_range()`, take the min/max), but a "365"
showed up as the computed ceiling in practice — almost certainly a
mis-scraped "365 days" PTO figure landing in the years-experience field
rather than a real 365-years requirement, not worth chasing down in
`blurb_extractor.py` for one outlier. A fixed 0-20 range
(`db.YOE_SLIDER_MAX`) sidesteps that class of bug entirely and reads more
predictably to users besides. This only changes where the slider's handles
start/end, not the actual filter behavior: the right handle sitting at its
max is already treated as "no ceiling requested" (see below), so a
genuinely-20+-years posting still surfaces exactly like it did before.

A filter is "active" only once a handle has actually moved off its
starting endpoint — dragging just the salary floor sends `salary_min` but
no `salary_max` (no ceiling requested), and touching neither slider sends
neither param at all, same as leaving a dropdown on "Any." `db.search_jobs()`
takes the new params through as a range-overlap test: a job passes if its
*own* posted range overlaps the requested range at all (a job posted
"$140k–$180k" still shows up under a "$150k+" filter, since $180k clears
the floor), not a strict containment check. Salary overlap runs in SQL
against the real `salary_min`/`salary_max` columns; YOE overlap runs in
Python post-fetch against the parsed `years_experience` text, using the
same `parse_years_range()` helper.

One deliberate departure from how this app treats missing data everywhere
else: the location filter gives jobs with no reported location the benefit
of the doubt and still shows them (see "Location filter" above), but a job
with no salary or YOE data gets *excluded* once the corresponding slider
filter is actually touched — most job boards handle salary filters this
way, and the reasoning holds here too: a candidate who deliberately
narrowed a $150k+ range wants confirmed matches, not unknowns padding the
count. Untouched sliders don't filter at all, so this only kicks in once
the user has opted in.

Verified against a small synthetic DB covering the edge cases: a job with
no salary data excluded once a salary filter is active; a job with no YOE
data excluded once a YOE filter is active; a job with an open-ended YOE
requirement ("10+") correctly excluded from a `[5, 8]` filter (no overlap)
while another open-ended posting ("8+") correctly passes the same filter
(8 is within `[5, 8]`); and `salary_bounds()`/`years_bounds()` both
reflecting the true min/max across the seeded rows, including the
open-ended-YOE ceiling fix above.

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

**Follow-up fix: the regex was giving up too easily on real postings.**
Pulled live job descriptions from several real Greenhouse boards
(Redwood Materials, Brex, Samsara, Instacart, Gusto — ~920 postings) to
audit `extract_salary()` against actual text rather than assumptions, and
found the extractor was quietly returning `(None, None)` on real,
extractable comp figures in two situations:

1. **"$X/yr to $Y/yr" format** (each number carrying its own `/yr` suffix,
   rather than one shared suffix at the end) broke the existing range
   regex entirely — the `/yr` in between the number and `to`/`-` isn't
   accounted for by a plain `<num> - <num>` separator pattern, so it fell
   through to `_SINGLE_RE` and only ever grabbed the two numbers as
   separate, unpaired single values. Added `_RANGE_YR_RE` specifically for
   this shape.
2. **First-match-only meant one bad number sank a whole valid range
   nearby.** A real Gusto posting had a typo — "`$179,000,000/yr to
   $220,000/yr` in Denver..., and `$210,000/yr to $260,000/yr` for San
   Francisco..." (clearly meant `$179,000` — the extra `,000` is a
   posting-authoring mistake). The old code called `.search()` once per
   pattern, got the `179,000,000` pair, correctly rejected it as
   unreasonable (`_valid_pair`), and then gave up on the WHOLE pattern
   instead of continuing on to the second, perfectly valid `$210,000-
   $260,000` range later in the same text. `extract_salary()` now scans
   every match of a pattern (`finditer`, not `search`) and returns the
   first one that passes `_valid_pair`, only moving to the next pattern if
   none of the current one's matches are valid — same idea multi-location
   postings need anyway (several pay bands listed for different offices).

Salary recall across that same ~920-job real sample went from roughly what
the original conservative extractor found to 692/923 (75%) with these two
fixes, with no new false positives — re-verified the existing CAD/"CA$"
guard (Canadian-currency ranges correctly still return `None` rather than
being mislabeled as USD) and the funding-amount guard (`$50,000,000 Series
B` still correctly rejected by `MAX_REASONABLE`) both still hold.

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

### Role blurbs, years-of-experience badge, and tools row

Each card shows a short blurb, a years-of-experience badge when the posting
states one, and a row of specific tools/tech the posting mentions — modeled
directly on hiring.cafe's cards (a real reference the user pointed at,
comparing our then-generic blurbs against theirs), with this app's own
visual styling. Same "best-effort, clearly extracted, not curated"
philosophy as salary throughout.

`blurb_extractor.py` tries, in order:

1. The first couple bullet points under a heading that looks like a real
   qualifications/responsibilities section — "Qualifications" /
   "Requirements" / "Desired Qualifications" / "Essential Duties" /
   "Responsibilities" / "You Should Have" / "What You'll Bring" / "Who You
   Are" / etc. Bullets go FIRST now (they didn't used to) because real
   bullet content is far more specific and useful than a single sentence
   built around wherever "years" happens to appear. For Lever specifically,
   this uses the `lists` field (already split into titled sections like
   "What We Require") instead of hunting for a heading in a wall of text.
2. A years-of-experience-anchored sentence, if no bullets were found.
3. The first substantive sentence of the description, as a last resort —
   actively skipping past an "About [Company]" / "Company Overview" / "Who
   We Are" intro block if that's what the posting opens with, rather than
   grabbing it (see below).

`extract_years_experience()` pulls just the bare number phrase ("5+",
"3-6") as its own field, rendered as a small badge next to the blurb text
instead of being buried inside a sentence.

**The bug that prompted this rewrite**: cards were showing things like
"About Redwood Materials — Redwood is localizing a global battery supply
chain..." as the blurb, identical across every posting from that company,
on postings that had perfectly good qualifications content further down.
Fetching a real Greenhouse posting and running it through the extractor
found two compounding causes:

1. **The heading regex was too strict.** It required an EXACT match like
   `<strong>Qualifications</strong>` with nothing else in the tag — no
   trailing colon, no prefix beyond "minimum/required/preferred/basic". A
   real posting's actual heading was `<strong>Desired Qualifications:
   </strong>` — "Desired" wasn't an allowed prefix and the trailing colon
   broke the match entirely, so it never fired. Broadened to cover common
   real-world phrasings ("Desired", "Additional", "Key", "Essential"
   prefixes; "Responsibilities", "Duties", "Must Haves", "You Should Have",
   "You Have", optional trailing colon) — validated against real postings
   from three different companies (Redwood Materials, Adyen, Digible)
   spanning Greenhouse content that used at least five different heading
   phrasings for the same kind of section.
2. **The last-resort fallback had no boilerplate guard.** With no
   qualifications heading found, it fell to "first sentence ≥60 chars" —
   and since companies routinely put "About [Company]" as the literal
   first paragraph of the job description (before any qualifications
   section), that's exactly what got grabbed. Fixed with
   `_skip_about_block()`: detects a company-intro heading ("About Us",
   "Company Overview", "Who We Are", "Our Mission", etc.) at the start of
   the text and skips past it — including multiple sentences of marketing
   copy if needed (one real example, Digible's "Company Overview:", ran
   3+ sentences of mission/culture copy before reaching anything
   job-specific; `_COMPANY_MARKETING_SIGNAL_RE` keeps dropping sentences
   that still read like generic company language, capped at 4, rather than
   assuming exactly one sentence is enough).

Verified against 9 real postings across those 3 companies (fetched live
from their Greenhouse boards, not synthetic text): 0 fell back to
company-boilerplate after the fix, versus roughly half before it.

`tools_extractor.py` is new: a curated list of ~55 real
tool/technology names (Salesforce, SQL, Python, Tableau, Kubernetes,
Figma, AI/LLM, etc., spanning the many job functions this app covers, not
just engineering) matched against the posting text, capped at 6 per
card, rendered as a row of small chips under the blurb with a wrench icon.
A few short/common-word tool names are deliberately excluded or narrowed
to a safer anchor phrase to avoid false positives in exactly the kind of
text this app scrapes a lot of — bare "Segment" would match "market
segment"/"customer segment" constantly in sales/marketing postings, and
bare "Linear" would match "linear regression"/"linear model" in
data-science postings, so those only match on their more distinctive full
product names/domains instead.

On an 800-company sample, 100% of jobs get some blurb (the fallback still
ensures that), and the qualifications-bullets / years-of-experience passes
now catch a large majority of them with something genuinely specific.

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

**Follow-up fix: the YOE regex was missing most real phrasings.** Same
~920-job live audit used for the salary follow-up above found
`extract_years_experience()` was only finding a mention in 43.7% of
postings that actually stated one — real Greenhouse text is full of
phrasings the original pattern didn't cover:

- **En/em dashes in ranges.** "1–5 years of industrial experience" (en
  dash) wasn't recognized at all — the separator only accepted a plain
  hyphen or the word "to". Widened to match `salary_extractor.py`'s
  existing dash handling (`-`/`–`/`—`/`to`).
- **Descriptive words between "years of" and "experience."** "5+ years of
  hands-on Python development experience," "6+ years of customer
  experience, including 1+ years in Payroll" — the original pattern only
  matched "years of experience" verbatim (at most one "relevant" in
  between), so any real domain/skill words in that gap broke the match.
  Now tolerates up to 4 intervening words, capped and restricted to a
  word-ish character class (no `.`) so it can't run past a sentence
  boundary hunting for a stray later "experience" — same
  anti-catastrophic-backtracking reasoning as the perf note above.
- **Lead-in cue phrases with no "experience" word nearby at all.**
  "Minimum 7+ years of industrial electrical experience," "at least 5+
  years of customer-facing pre-sales experience," "more than 5 years" —
  added explicit handling for "minimum (of)/at least/more than N years,"
  and treat these as open-ended ("5+") even when the source text has no
  literal "+" on the number, since the cue phrase itself already means "N
  or more."

Recall went from 403/923 (43.7%) to 634/923 (68.7%) on the same real
dataset, with no measurable perf regression (0.32ms/job — the anti-
backtracking design from the note above held up under the wider pattern)
and zero false positives found on manual review of a random sample of the
newly-caught matches (checked specifically for company-age/funding
language like "founded," "in business," "raised $" sneaking in via the
loosened "N years" matching — none did).

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

Computed entirely server-side now, in `db.py`'s `_match_info()`, and
attached as a `match_tier` field on every job the API returns once a resume
is active (independent of sort order — the frontend badges every visible
card, not just when sorted by match). `app.js` just reads `job.match_tier`
straight off the response; there's no client-side recomputation anymore, so
there's exactly one place this logic lives.

This replaced an earlier version (client-side, `matched_terms /
len(all_terms)` against title+blurb, ≥50%/≥20%/else thresholds) that in
practice badged almost nothing above "poor," even for resumes with an
obvious, direct fit. Two compounding bugs, both worth knowing about if this
ever regresses again:

1. **Shared term budget.** `resume_parser.suggest_query()` used to return
   one merged list — extracted titles, skills, AND synonym expansions — all
   competing for a single 25-term cap. A resume with a normal-sized
   skills/tools section (Salesforce, SQL, Tableau, Excel, ...) could fill
   that cap before the far more predictive title-derived terms (the actual
   title plus its role-synonym expansions) were even added. Fixed by
   splitting the return value into `title_terms` (capped separately at 40)
   and `skill_terms` (capped at 15) — see the docstring in
   `resume_parser.py`.
2. **Ratio scoring against too narrow a haystack.** Even with better terms,
   scoring `matched / len(terms)` against title+blurb punished a long,
   diverse term list: skill/tool names essentially never appear in a job
   title or in the ~1-2 sentence qualifications blurb this app scrapes, so
   they mostly just inflated the denominator and dragged genuinely
   on-target jobs' ratios below the 20%/50% cutoffs. Fixed by switching to
   direct hit-count tiers instead of a fraction (a long tail of extra
   synonym terms can now only help, never dilute), and by adding the job's
   `department` field to the haystack — a short, structured ATS label
   ("Revenue Operations," "Sales," "Engineering") that's often a far more
   reliable signal than free-text blurb content, and wasn't being checked
   at all before.

Current tiering: **best** = at least one `title_term` (extracted title or a
role-synonym expansion of it, excluding `_GENERIC_TERMS` below) appears
directly in the job's title. **good** = no direct specific title hit, but a
title_term shows up in blurb/department, a bare generic term hits the
title, or 2+ skill_terms show up anywhere. **poor** = none of the above.

That first fix overcorrected, though — the very next resume upload came
back with essentially every posting badged "best match" (dozens of result
pages). Root cause was one line up the stack, in
`resume_parser.py`'s `ROLE_NOUN_RE`: the "leading Title-Case words" part of
the title-phrase regex used a `{0,3}` quantifier, which allows *zero*
leading words. Combined with the role-noun alternation being
case-insensitive, that meant a completely ordinary, lowercase, out-of-
context sentence like "worked with the sales **lead**" or "reported to the
finance **controller**" got extracted as a standalone 1-word "title
phrase" — just `lead`, just `controller` — with no capitalization check
left to stop it once the leading-words group matched nothing. Those bare,
maximally generic words then matched almost every job title on the board
(nearly any posting has "lead" or "manager" or "director" in it
somewhere), which is exactly what produced page after page of false
"best" badges. Fixed at the source: `ROLE_NOUN_RE` now requires `{1,3}` (at
least one genuine leading Title-Case word), plus a defense-in-depth
`len(words) >= 2` check in `_extract_title_phrases()` in case buzzword/
section-header stripping ever whittles a phrase down to one word again.
`db.py`'s `_GENERIC_TERMS` carve-out (a handful of role_synonyms.py groups
still deliberately include a bare catch-all like "Operations" in their
`related` list) was also tightened so those can only ever produce a "good"
match, never single-handedly a "best" one — belt-and-suspenders against
the same failure mode recurring from the synonym side instead of the
extraction side.

One more small, low-stakes extraction bug caught in the same pass: "PRQ
lead **time**" (a supply-chain lead-time metric, not a job title) was
matching as a 2-word title phrase ending in "Lead," since an all-caps
acronym like "PRQ" satisfies the leading-Title-Case-word check just as
well as a real word does. Fixed with a narrow, specific guard in
`_extract_title_phrases()`: a phrase ending in "lead" is dropped if the
very next word in the source text is "time." Doesn't touch legitimate "X
Lead" titles (Product Lead, Team Lead, Engineering Lead) at all.

Verified three ways: a synthetic 20-job batch (10 relevant RevOps/BizOps/
GTM-family roles vs. 10 unrelated ones — 9-10 best, the rest correctly
poor, no false positives); a regression batch specifically targeting the
"bare word from prose" bug (confirms `lead`/`controller`/`director`/
`analyst`/`manager` never again appear as standalone extracted terms); and
a batch built from Jared's actual resume content/bullets (reconstructed
from the `jared-resumes` skill, since the source PDFs aren't available in
this environment) against a mix of real target-role titles (Director of
Revenue Operations, Chief of Staff, Technical Program Manager, Deal Desk
Manager, GTM Strategy Lead, PMO Lead, etc.) and clearly unrelated ones
(Registered Nurse, Electrician, Paralegal, ...) — 12/12 relevant roles
correctly flagged best/good, 0/10 false positives among the unrelated
batch.

That last pass also surfaced a real synonym-coverage gap, not a bug: a
resume titled "Director, Strategy & Business Operations" wasn't pulling in
"Chief of Staff" postings, even though the `jared-resumes` reference notes
BizOps/Strategy framing as a strong real-world Chief-of-Staff match. The
`chief of staff` synonym group already expanded outward to Business
Operations, but not the other way around. Fixed by adding "Chief of Staff"
and "Special Projects" to the business-operations group's `related` list
too (and adding `strategy & operations`/`strategy and operations`/
`strategy & business operations` as additional triggers for that same
group), so the expansion now works in both directions.

### Location-aware filtering (excludes, doesn't just badge)

**Current behavior, read this first if the section below looks like it
contradicts itself:** as of the latest round of feedback, a job whose
location doesn't work for the candidate (not viable for a US-based
candidate at all, or an onsite role outside their home metro) is removed
from search results ENTIRELY, not just badged "poor" and left visible at
the bottom of the list. This was a deliberate choice, made after the
original "poor but still visible" design (described in detail below, kept
for the history) drew the opposite complaint in practice: onsite roles the
candidate can't actually take were still cluttering the results.

The exclusion logic itself is unchanged from the "poor" classification
below — same `is_clearly_non_us()` / metro-distance rule, same
`resume_us_based`/`resume_metro_terms` gating so it only ever kicks in
when we're confident about the candidate's location. What changed is
`_is_location_excluded()` is now applied as a hard filter on the candidate
list in `search_jobs()`, before match-tier scoring, rather than inside
`_match_info()` as a tier override. `_match_info()`'s "poor" tier is now
purely about weak title/skill overlap — a job that reaches scoring at all
has already had its location judged fine (or location wasn't a factor
because the candidate's location wasn't confidently known).

Worth remembering if this ever gets revisited: the earlier "resume upload
showing too few total results" bug (see below) is exactly the failure
mode to watch for if this filter ever ends up layered together with other
automatic narrowing — that bug came from stacking an auto-applied query
narrowing AND an auto-applied location filter, which compounded into a
600-job search returning 7 results. This filter is deliberately scoped to
*only* remove jobs whose location is actually wrong for the candidate,
never title/skill mismatches (a weak-title-match "Registered Nurse"
posting in the candidate's own city still shows up, just tagged "poor" —
only genuinely wrong-location jobs get removed).

### Location-aware match tiers (superseded by the exclusion above — kept for history)

Title/skill overlap alone can still badge a job "best match" that's
obviously off for the candidate — the case that surfaced this: a Phoenix-
based resume got "best match" badges on a Peterborough, UK role, two
"Remote (Canada)" roles, and a Prague role, purely because the job titles
lined up. None of those are viable for a US-only candidate, but the old
`_match_info()` had no concept of location at all.

Fixed with a new `location_groups.is_clearly_non_us(location)` classifier
and a `resume_us_based` flag threaded end-to-end: `resume_parser`/`app.py`'s
`/api/parse-resume` already tries to extract a US city from the resume
(see "Resume location auto-populate" below); `app.js` now remembers
whether that succeeded (`resumeUsBased = Boolean(data.matched_city)`) and,
only when it did, sends `resume_us_based=1` on every `/api/jobs` call
alongside the existing resume term params. `app.py` reads that into
`db.search_jobs(resume_us_based=...)`, which passes it through to
`_match_info(..., us_based=...)`. When `us_based` is true, any job whose
location reads as clearly non-US — a specific foreign city with no US
signal at all ("Prague", "Peterborough"), or an explicitly non-US remote
label ("Remote (Canada)"/"(UK)"/"(Europe)") — is force-set to `"poor"`
before any title/skill scoring happens, regardless of how well the title
matches. Blank locations and unqualified/ambiguous "Remote" (no region
specified) are deliberately given the benefit of the doubt and left alone,
since they might still be US-eligible. If the resume parser couldn't
confidently place the candidate in the US, `resume_us_based` stays false
and location is left out of scoring entirely — same behavior as before
this fix, rather than guessing.

Verified end-to-end against a small synthetic DB: three otherwise-
identical "Business Operations Manager" postings, one each in Prague,
"Remote (Canada)," and Phoenix, AZ. With `resume_us_based=True`, only the
Phoenix posting scores `"best"`; both Prague and Remote-Canada drop to
`"poor"`. With `resume_us_based=False` (or omitted), all three still score
`"best"` — confirming the gate only engages when the app is actually
confident about the candidate's location, not by default.

**Follow-up: onsite-but-wrong-metro roles were still slipping through.**
The fix above only ever checked "is this US-viable at all" — it stopped
Prague and Remote-Canada, but real feedback showed it wasn't nearly
enough: a Phoenix, AZ resume was still getting "best match" on onsite
roles in Denver, Salt Lake City, San Francisco, and Tysons, VA, plus a
"Remote, Mexico" posting — all technically-US-passable-or-unrecognized
locations that a Phoenix-based candidate can't actually take without
relocating. Two separate bugs, both fixed:

1. **LatAm wasn't a recognized disqualifying remote region.**
   `is_clearly_non_us()` only special-cased Canada/UK/Europe as explicitly
   non-US remote labels; anything else (including "Remote, Mexico") fell
   into the "unqualified remote, benefit of the doubt" branch and was
   never flagged. Added a `LATAM_COUNTRY_NAMES` set (Mexico, Brazil,
   Argentina, Colombia, etc.) and a `_mentions_latam()` check, same
   pattern as the existing Canada/Europe checks. APAC is still an
   acknowledged gap — there's no classifier for it yet, so an unlabeled
   "Remote - APAC" still wouldn't be caught.
2. **Onsite roles had no proximity check at all.** `_match_info()` gained
   a `metro_terms` parameter: lowercased "city, st" strings for the
   candidate's home metro, reusing the exact same nearby-metro list
   `metro_areas.py` already builds for the "Narrow to Phoenix, AZ area"
   suggestion button (see "Resume location auto-populate" below). When
   `us_based` is true and `metro_terms` is given, a job is also demoted to
   `"poor"` if it's **not remote at all** (remote roles are exempt — they
   were already filtered down to US-viable-or-ambiguous ones by the
   `is_clearly_non_us` check) and its location **doesn't contain any of
   the candidate's metro cities** as a substring. Threaded through the
   same way as the other resume flags: `/api/parse-resume`'s
   `location_terms` response is captured into `app.js`'s
   `resumeMetroTerms`, sent as repeated `resume_metro_term` params
   whenever a resume is active, read by `app.py`, and passed into
   `db.search_jobs(resume_metro_terms=...)`.

   Hit a real matching bug while wiring this up: metro_areas.py's curated
   lists are all written as abbreviated state codes ("Phoenix, AZ"), but
   real scraped job locations often spell the state out in full
   ("Phoenix, Arizona, United States") — a literal "Phoenix, AZ, United
   States" resume role never matches "phoenix, az" as a plain substring
   against "phoenix, arizona, united states". A synthetic test that
   included the literal same-city job caught this immediately (it came
   back `"poor"`, which is exactly backwards). Fixed with a new
   `location_groups.city_state_variants()` helper and a
   `STATE_ABBR_TO_NAME` lookup table: each metro term is expanded to both
   spellings before matching, so either format works.

   Verified against the exact screenshot that reported this: a Phoenix
   metro-term list against onsite postings in Denver, Tysons VA, San
   Francisco, Salt Lake City, and "Remote, Mexico" (all now correctly
   `"poor"`), alongside onsite-in-Phoenix (both state-abbreviated and
   full-name spellings), "Remote - US", and unqualified "Remote" (all
   correctly still `"best"`). Also re-ran the earlier Prague/Remote-Canada
   regression test and the salary/YOE range-filter tests afterward to
   confirm neither this fix nor the LatAm change broke anything upstream.

### Match sort (server-side)

"Best match" isn't only a badge — it's also a sort option (`#sort-match-
option` in `index.html`, hidden until a resume is uploaded, then selected
automatically). Badges alone can only describe whatever's on the current
page; ranking the *full* result set by match quality has to happen
server-side, before pagination. `db.search_jobs(sort="match",
resume_title_terms=[...], resume_skill_terms=[...])` computes `match_tier`
+ an ordinal score for every candidate row via `_match_info()`, then sorts
by that score descending, with newest-posted-first as the tiebreaker within
a tier — implemented as two sequential stable `.sort()` calls (sort by date
first, then by score, since Python's sort is stable and a later sort only
reorders elements that tied on the earlier one). `SORT_OPTIONS` in `db.py`
deliberately excludes `"match"`, so it falls through to the default
newest-first SQL ordering when no resume terms are present — the Python
resort only kicks in when at least one of the two term lists is actually
populated.

### Resume keyword expansion (role synonyms)

Keyword search against the literal phrases on a resume alone misses a lot
of real postings for the same kind of work: a resume that says "Revenue
Operations Manager" won't keyword-match a posting titled "RevOps Manager"
or "Sales Operations," even though they're the same job. `role_synonyms.py`
is a hand-curated dictionary of ~24 role families (RevOps/Sales Ops/GTM,
Chief of Staff, Product Management, Program Management, Growth/Product
Marketing, Sales/BDR/SDR, Customer Success, Data Analytics, Data Science,
Software Engineering, UX/Product Design, People Ops/Talent Acquisition,
FP&A/Accounting, Supply Chain, Operations, Strategy/Consulting, IT/SysAdmin,
Legal, Executive Assistant, etc.) — each maps a set of trigger substrings to
a set of related terms. `expand_with_synonyms()` matches an extracted
resume phrase against every group's triggers and returns the related terms
not already present.

`resume_parser.suggest_query()` returns three things: `title_terms`
(extracted titles + every synonym expansion of them, capped at 40 — the
primary match-scoring signal, see above), `skill_terms` (extracted
skills-section items, capped at 15 — a secondary, weaker signal), and
`query_string` (built from only the first 8 originally-extracted
title/skill phrases, NOT synonym expansions — short and legible, since the
user is expected to review/edit it before searching). Keeping title and
skill terms in two separately-capped lists, rather than one merged bag, is
what fixed the sparse-match bug described above.

Title-phrase extraction itself (`_extract_title_phrases()`) also got more
careful: the original regex was fully case-insensitive, which meant any
lowercase word (like "with") could satisfy its "capitalized leading word"
requirement, producing garbage matches like "Partnered with Sales Ops" as a
3-word title phrase. Fixed by scoping case-insensitivity to only the
role-noun alternation (`(?i:...)` inline group) while keeping the leading
words genuinely case-sensitive, plus a `BUZZWORD_RE` pass that strips
resume-summary openers ("Results-driven", "Experienced", "Proven", etc.)
that are capitalized only because they start a sentence, not because
they're part of an actual title.

### Resume location auto-populate

`resume_parser.extract_location()` looks for a "City, ST" pattern
(case-sensitive 2-letter USPS state code, to avoid common lowercase words
like "or"/"me" false-matching), biased toward the first ~600 characters of
the document since contact info almost always lives at the very top.
`metro_areas.py` is a curated dataset — not real geocoding, same tradeoff as
`location_groups.py` and `role_synonyms.py` — mapping the ~65 largest US
metro home cities to a hand-picked list of nearby suburbs/cities
approximating "within ~50 miles" (e.g. "Phoenix, AZ" → Phoenix, Scottsdale,
Tempe, Mesa, Chandler, Gilbert, Glendale, Peoria). A city outside the
curated set still contributes its own literal "City, ST" as a single
location term rather than nothing.

On a successful resume parse, `POST /api/parse-resume` returns
`location_terms` (the nearby-city list, or just the one matched city) and
`location_groups: ["remote_us"]`.

**This used to auto-apply both the location chips AND a narrow title query
to the search on upload, and it badly over-filtered.** The very next round
of testing after building this surfaced it directly: a resume that should
have matched hundreds of open roles came back with only ~30 total results.
The mechanism, confirmed with a synthetic 600-job test: the auto-filled
query box (an OR of the 8 extracted title phrases, matched against title
text only) cut 600 → 109 on its own; the auto-added location chips
(nearby-metro cities + Remote (US)) on top of that cut it further to 7 —
excluding every onsite job outside the resume's home metro, and every
remote posting whose location string didn't classify as `remote_us`. Once
server-side match-tier sorting existed (see "Match sort" above), that
auto-narrowing became actively counterproductive: it fights the exact
thing match-tier sort is for, which is ranking the FULL dataset by fit so
the best matches float to the top without hiding everything else.

Fixed by making both opt-in instead of automatic: on upload, the query box
and location filter are left untouched, `sort` is set to `match`, and the
search fires immediately — so all open roles show up, ranked best-match-
first. The extracted query string and location suggestion are still
surfaced, as two small pill buttons in the resume-status line ("Narrow to
these titles" / "Narrow to Phoenix, AZ area + Remote (US)") the user can
click if they specifically want to narrow further — `applyQueryBtn`/
`applyLocationBtn` in `handleResumeFile()` in `app.js`.

### Response time

Reported symptom: ~10s between clicking search (or uploading a resume) and
results showing up. Two backend inefficiencies were found and fixed
regardless of whether they were the dominant cause:

1. `tools` (stored as a JSON string in SQLite) was being `json.loads()`'d
   for every candidate row right after the SQL fetch — up to `MAX_CANDIDATES`
   (50,000) rows — even though only the ~25-50 rows on the current page ever
   reach the response. Deferred to only run on `page_rows`.
2. Match-tier scoring (`_match_info()` over every candidate) had the same
   shape of waste when `sort != "match"`: it doesn't affect ordering in that
   case, so there's no reason to compute it for rows that never make it into
   the response. Deferred to `page_rows` only; still runs on the full
   candidate set when `sort == "match"`, since that's genuinely needed to
   determine the sort order before pagination.

Before optimizing further, these were benchmarked locally against 60,000
synthetic rows with realistic term counts
(`search_jobs(sort="match", resume_title_terms=[26 terms],
resume_skill_terms=[11 terms], resume_us_based=True, per_page=50)`): 773ms.
That's real but far short of 10 seconds, so the Python-side scoring loop is
not, by itself, the main source of the reported latency.

A concrete frontend bottleneck was also found and fixed: page load used to
call `loadLogoCache().then(() => search(1))` — a fully sequential chain that
blocked the very first search behind fetching and parsing the entire
~104KB company-logo cache JSON (one entry per company, ~4,400 companies).
`search()` now fires the jobs request immediately and only waits on the
logo cache the first time (via `Promise.all`, see `logoCacheLoaded` in
`app.js`); subsequent searches don't touch the logo fetch at all.

Honest caveat: it's not confirmed these two fixes fully account for the
reported ~10s. Render's free-tier cold-start behavior (the dyno spinning
down after inactivity and taking several seconds to wake back up) is a
plausible additional contributor that's outside the codebase and wasn't
investigated in this pass — worth checking if slowness persists,
particularly on the very first request after a period of no traffic.

### Results layout

Cards are laid out in a responsive grid — 5 across at full desktop width,
stepping down to 4 / 3 / 2 / 1 as the viewport narrows (see `.results-grid`
in `style.css`). The site's overall max-width grew from 1100px to 1360px to
give 5 columns reasonable breathing room.

Pagination shows numbered page buttons now instead of just Prev/Next
arrows — `pageNumbers(current, total)` in `app.js` builds a classic
"windowed" list: page 1, the last page, and a small window 2 pages either
side of the current one, collapsing any bigger gap into a single "…" (a
gap of exactly one page shows that page number instead, since an ellipsis
there wouldn't save any space). Caps out around 9 visible buttons even
across hundreds of pages of results, rather than either rendering every
page number or leaving arrows as the only way to jump around.

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
   - `.python-version` pins the build to Python 3.12 — deliberately, not
     Render's newest default. Hit this the hard way: Render defaulted to
     3.14 (its current latest), which has no matching `psycopg2-binary`
     wheel for the pinned `2.9.9` version in `requirements.txt`. That import
     failure was getting silently swallowed by `db_users.py`'s
     `except ImportError: psycopg2 = None` (fixed to at least log now, but
     the real fix is avoiding the mismatch in the first place) — the
     symptom was `DATABASE_URL` looking completely correct in the Render
     dashboard while the app insisted it wasn't set. If you ever bump
     `psycopg2-binary` to a version with real 3.14 wheels, this pin can go.
4. Start command: (auto-detected from `Procfile`) or set explicitly:
   `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
5. Add a **persistent disk** (Render dashboard > Disks) mounted at, e.g.,
   `/data`, and set the environment variable `JOBS_DB_PATH=/data/jobs.db` so
   the SQLite cache survives restarts/redeploys. Without a persistent disk
   the app still works, it just re-scrapes from empty on every redeploy.
6. Optional environment variables:
   - `SCRAPE_INTERVAL_HOURS` (default `8`)
   - `SCRAPE_MAX_WORKERS` (default `4`)
   - `DATABASE_URL` / `SECRET_KEY` — see "User accounts" below; the site
     runs fine without either, just without accounts.
   - `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` — optional bot check on
     signup, see "Abuse guardrails" under "User accounts" below; the site
     runs fine without either, just without the CAPTCHA widget.
   - `RESEND_API_KEY` / `RESEND_FROM_EMAIL` / `APP_BASE_URL` — optional
     password reset, see "Password reset" under "User accounts" below; the
     site runs fine without these, just without a "Forgot password?" link.
   - `GA_MEASUREMENT_ID` — optional Google Analytics tracking, see
     "Analytics" below; the site runs fine without it, just without
     traffic tracking.

## User accounts (saved searches, applied-job tracking)

Optional, and off by default — the site works exactly as before (search,
resume match, filters) with no account system configured at all. Turning
it on adds a login, a "Save this search" button, and a "Mark applied"
toggle on every job card.

The login/signup modal (and the forgot-password/reset-password modals) got
a dedicated visual treatment (`.modal-auth` in `style.css`) — a dark
branded header with the site's diamond mark, icon-prefixed email/password
fields — distinct from the plain `.modal-title` look the saved-searches and
applied-jobs modals use, since this is the one modal most first-time
visitors actually form an impression of the site from.

### Why this is a separate database from the job cache

`db.py`'s SQLite `jobs.db` is disposable by design — if it's ever lost,
the next scrape rebuilds it from scratch, and it already gets pruned/
rewritten constantly (see `prune_stale`, `upsert_jobs`). A user's account
and the searches/applications they've saved are NOT disposable — losing
them actually costs the person something. So accounts live in their own
Postgres database (`db_users.py`), connected via the standard
`DATABASE_URL` env var (`postgresql://user:pass@host:port/dbname`),
completely separate from the SQLite file and its lifecycle.

Render's **free** Postgres tier auto-deletes the whole database after 30
days with no warning — fine for a disposable cache, not something to put
irreplaceable account data on. The recommended setup uses Render's
smallest **paid** Postgres tier (Basic, ~$6/month as of writing — check
Render's current pricing), which doesn't expire. At the data volumes a
tool like this actually sees (a saved search is roughly 1KB; even a few
hundred applied-job records per user is well under 100KB), that tier has
a lot of headroom — this isn't a case where the database is likely to
become the bottleneck or need upsizing any time soon.

### Setup

1. In Render: **New > PostgreSQL**, pick the paid Basic tier (not Free,
   for the reason above), same region as the web service.
2. Copy its **Internal Database URL** from the Render dashboard.
3. On the web service: **Environment**, add:
   - `DATABASE_URL` = the internal connection string from step 2.
   - `SECRET_KEY` = any long random string (e.g. `python3 -c "import secrets; print(secrets.token_hex(32))"`).
     This signs login session cookies — **if it's not set, the app still
     runs, but generates a random one on every process start/restart,
     which silently logs every user out each time the app restarts.** Not
     a problem for local dev; a real problem on a host that
     redeploys/restarts periodically.
4. Redeploy. The three account tables (`users`, `saved_searches`,
   `applied_jobs`) are created automatically on startup
   (`db_users.init_db()`) — no manual migration step.

Locally, without a `DATABASE_URL` set at all, every account-related route
(`/api/signup`, `/api/login`, saved searches, applied jobs) returns a
clean `503 {"error": "Accounts aren't set up on this deployment yet."}`
instead of crashing — verified directly, along with the fact that plain
search and the homepage both keep working normally in that state. The
frontend just doesn't show any account UI when `/api/me` comes back
logged-out, so a deployment without accounts configured looks and
behaves exactly like it did before this feature existed.

### Abuse guardrails (rate limiting + optional bot check)

Two independent layers, both aimed at bot signup floods rather than a
sophisticated attacker — the goal is "cheap to add, closes the obvious
gap," not a hardened auth system:

1. **Rate limiting** (`Flask-Limiter`, always on, no configuration needed):
   `/api/signup` is capped at 5 attempts/hour per IP, `/api/login` at 10/minute
   per IP. Both return a `429 {"ok": false, "message": "Too many attempts..."}`
   once tripped. Uses in-memory storage (`storage_uri="memory://"` in
   `app.py`) — correct for the single web-service instance this app runs as;
   if this ever scales to multiple instances behind a load balancer, the
   limits stop being shared across them and a Redis `storage_uri` should be
   added (Flask-Limiter supports this as a drop-in config change).
2. **Cloudflare Turnstile** (optional, off unless configured): a lightweight,
   free CAPTCHA alternative shown on the signup form only (not login).
   Controlled by two env vars:
   - `TURNSTILE_SITE_KEY` — public, safe to expose; served to the frontend via
     `GET /api/auth-config`.
   - `TURNSTILE_SECRET_KEY` — private; used server-side in `verify_turnstile()`
     to check the token against Cloudflare's `siteverify` endpoint.

   Get both free at the [Cloudflare dashboard](https://dash.cloudflare.com/)
   → Turnstile → Add site (no domain ownership/DNS setup required — Turnstile
   works on any domain you register it for). Leave both env vars unset and
   the signup form just skips rendering the widget entirely, and
   `verify_turnstile()` skips the check server-side to match — same
   graceful-degradation pattern as `DATABASE_URL`/accounts.

   Verified against Cloudflare's real `siteverify` endpoint (not mocked)
   using their [published dummy sitekey/secret pairs for
   testing](https://developers.cloudflare.com/turnstile/troubleshooting/testing/):
   an always-pass pair correctly returns success, an always-fail pair
   correctly returns failure, and a configured-but-missing-token request is
   correctly rejected before ever calling Cloudflare. Also verified the
   unconfigured case (no secret set) skips the check and lets signup proceed
   normally.

Why these two and not more: the actual cost exposure from spam signups on
Render's pricing model is small (see reasoning below) — compute is a flat
monthly rate, not per-request, and each fake account is a few hundred bytes
in Postgres. The realistic risk is annoyance (fake accounts cluttering the
`users` table) rather than a runaway bill, so a free rate limiter plus an
optional free CAPTCHA is proportionate; something heavier (email
verification, phone verification) wasn't worth the added signup friction for
what this app needs today.

### Password reset

Optional, same pattern as everything else here: off unless configured,
nothing breaks if it isn't. Uses [Resend](https://resend.com) (free tier:
3,000 emails/month, 100/day — more than enough for this) to send the reset
email, via a plain `urllib` POST to Resend's REST API (`app.py`'s
`send_password_reset_email()`) rather than pulling in their SDK for one
call — same pattern as `verify_turnstile()`'s Cloudflare call.

**Setup:**

1. Create a free account at [resend.com](https://resend.com), grab an API
   key.
2. **Verify a domain you own** in Resend's dashboard (Domains → Add
   Domain, then add the DNS records they give you). This step matters:
   until a domain is verified, Resend restricts your account to sending
   *only* from `onboarding@resend.dev`, and *only* to the email address you
   signed up to Resend with — fine for testing it yourself, useless for
   sending real users a reset link. A subdomain works fine
   (`mail.yourdomain.com`) if you don't want to touch your main domain's
   DNS.
3. On the web service, set:
   - `RESEND_API_KEY` = the API key from step 1.
   - `RESEND_FROM_EMAIL` = e.g. `Skip The Boards <noreply@yourdomain.com>`,
     using the domain verified in step 2. Defaults to
     `Skip The Boards <onboarding@resend.dev>` if unset, which — per the
     restriction above — will silently only work for emailing yourself.
   - `APP_BASE_URL` = your deployed URL (e.g.
     `https://open-roles-finder.onrender.com`), used to build the link
     inside the reset email. Falls back to Flask's `request.url_root` if
     unset, which is fine for local dev but can be wrong behind a
     proxy/load balancer in production.
4. Redeploy. The "Forgot password?" link appears on the login form
   automatically once `RESEND_API_KEY` is set (checked via
   `GET /api/auth-config`); a `password_reset_tokens` table is created
   automatically on startup, same as the other account tables.

**How it works:** requesting a reset (`POST /api/forgot-password`) always
returns the same generic "if that email has an account..." response,
whether or not the email is actually registered — same anti-enumeration
reasoning as the login error message. If it is registered, a random
32-byte token is generated, only its SHA-256 hash is stored (so a leaked
database dump alone can't be used to reset anyone's password — same
reasoning as `password_hash`), and it expires in 1 hour
(`RESET_TOKEN_TTL_HOURS` in `app.py`). Requesting a new reset link
invalidates any earlier unused one for that account, so only the most
recent email's link ever works. Clicking the emailed link lands on
`/reset-password?token=...`, which is the same single-page app — `app.js`'s
`checkForResetToken()` notices the `token` query param on load and opens
the "set a new password" modal directly rather than making the user find a
login button first. Submitting a new password (`POST /api/reset-password`)
validates the token (unused + unexpired), updates `password_hash`, and
marks the token used so it can't be replayed. Both endpoints are rate
limited (3/hour and 10/hour per IP respectively) on top of the token's own
unguessability.

Both routes are also gated behind `accounts_required` (same as every other
account route), so if `DATABASE_URL` isn't set at all, they return the
standard 503 rather than a different error.

### What's deliberately NOT built yet

**Applied-job tracking is a single toggle**, not a status pipeline
(applied / interviewing / offer / rejected) — deliberately kept simple
for v1. `applied_jobs` is a plain `(user_id, job_url, applied_at)` table;
extending it to a status enum + notes field later is a small, additive
schema change, not a redesign.

**Saved searches restore the plain filters** (query text, days,
department, commitment, sort, location chips, and the salary/YOE sliders
if touched) but NOT anything resume-derived — uploading a resume and
using "sort by match" isn't something a saved search remembers, since
that's tied to whatever resume happens to be uploaded in that browser
session, not a standing preference. Re-running a saved search always
falls back to newest-first if it had been saved while sorted by match.

### How this was tested

No live Render Postgres instance was available to test against directly,
so this was verified against a *real* local PostgreSQL server instead of
mocking the database calls out — a portable, no-root-required Postgres
18.6 binary (aarch64 build, from the `theseus-rs/postgresql-binaries`
project) was downloaded, initialized, and run on a throwaway port for the
duration of testing. Against that real instance, end-to-end via actual
HTTP requests (not calling Python functions directly): sign up, confirm
`/api/me` reflects the new session, save a search and list it back,
mark a job applied and confirm it shows `applied: true` on that URL (and
`false` on a different one) in a live `/api/jobs` response, fetch the
full "My Applications" list (including a URL deliberately marked applied
that was never in the job cache at all, to confirm the "delisted, but
still shown" behavior works), unmark it, log out, confirm `/api/me`
reflects the logged-out state, confirm a protected route returns 401
without a session, confirm a wrong password returns 401, and confirm
signing up twice with the same email returns 409 rather than a duplicate
account. Separately, confirmed the whole app still starts and serves
search/homepage normally with `DATABASE_URL` completely unset, and that
account routes return a clean 503 in that state instead of an unhandled
exception.

**Password reset, tested the same way** (real local Postgres, real HTTP
requests, plus a real call to Cloudflare's actual Turnstile `siteverify`
endpoint for the bot-check work below): signed up a user, requested a
reset (with a dummy `RESEND_API_KEY` so the token gets created and the
send attempt fails harmlessly, logged but not surfaced to the caller —
confirming the "email delivery failing shouldn't break the response"
design), captured the raw token server-side, submitted it to
`/api/reset-password` with a new password, confirmed the old password now
returns 401 and the new one returns 200, and confirmed replaying the same
(now-consumed) token a second time correctly fails with "invalid or
expired" rather than silently succeeding again. Also confirmed the
`DATABASE_URL`-set-but-`RESEND_API_KEY`-unset case: `/api/forgot-password`
still returns its normal generic response without attempting to send
anything or raising, and `/api/auth-config` reports
`password_reset_enabled: false` so the frontend never shows a "Forgot
password?" link that would just dead-end.

Rate limiting (5/hour signup, 10/minute login, 3/hour forgot-password,
10/hour reset-password) was verified by firing requests past each limit
and confirming the exact request that crosses the threshold — and only
that one — gets the `429` response instead of the normal one. Turnstile's
`verify_turnstile()` was checked against Cloudflare's real endpoint (not
mocked) using their published dummy sitekey/secret pairs for automated
testing: an always-pass pair returns success, an always-fail pair returns
failure, a configured-but-missing-token request is rejected before ever
calling Cloudflare, and the fully-unconfigured case (no `TURNSTILE_SECRET_KEY`)
skips the check entirely and lets signup through.

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

## Analytics (optional Google Analytics)

Off by default, same pattern as everything else in this README. Set
`GA_MEASUREMENT_ID` (looks like `G-XXXXXXXXXX`, from a GA4 property's
**Admin → Data collection → Web stream**) as an env var on the web
service, and every page load starts sending traffic to that property —
nothing else to configure, no code changes needed.

Deliberately not baked into `static/index.html` as a hardcoded `<script>`
tag, even though that's the "standard" way Google's own install
instructions show it — that would mean the measurement ID lives in the
repo (and in this zip, and in git history) rather than only in Render's
env vars. Instead, `GET /api/site-config` exposes the ID (it's a public
tracking identifier, not a secret, so this is safe), and `app.js`'s
`loadSiteConfig()` injects the two `gtag.js` script tags into `<head>` at
runtime only if it got a non-empty ID back. A deployment without
`GA_MEASUREMENT_ID` set just never touches Google's tracking script at
all — verified directly: `/api/site-config` returns
`{"ga_measurement_id": ""}` when unset and the real ID when set, checked
against a live local run of the app in both states.

## Contact form (optional, Resend)

Off unless both `RESEND_API_KEY` (same key used for password reset above)
and `CONTACT_EMAIL` are set. `CONTACT_EMAIL` defaults to
`jarededberg@gmail.com`; override it via env var if that should ever
change.

Contact is a real standalone page (`static/contact.html`, served by
`/contact` in `app.py`), not a modal — same treatment as `/faq` and
`/about` below: linkable, indexable, doesn't need the homepage loaded
first. The page's own inline script fetches `GET /api/site-config` (which
exposes `contact_enabled`/`contact_email` — the address is public anyway,
it's already in the footer byline/LinkedIn) and shows a real form (reason
dropdown, name/email/message, `POST /api/contact`, rate limited to
3/hour/IP same as forgot-password) if `contact_enabled` is true, or a
`mailto:` link to `contact_email` if it's false — never a dead end either
way. Submissions are emailed to `CONTACT_EMAIL` via Resend with
`reply_to` set to the sender's own address, so replying from your inbox
goes straight back to them. The reason dropdown (`CONTACT_REASONS` in
`app.py`) is validated server-side and falls back to "Other" if missing
or tampered with; name/email/message are HTML-escaped before being
embedded in the outgoing email body.

## Admin dashboard (signup tracking)

`/admin` shows total account signups and a per-day chart, for a quick
answer to "how many people have signed up." It's a real page
(`static/admin.html`, served by `/admin` in `app.py`) but the page itself
carries no auth check — it just calls `GET /api/admin/stats` on load and
shows an "unauthorized" state if that call 401s/403s, same shell-vs-data
split as the rest of the account system.

The actual gate is server-side: `/api/admin/stats` is stacked with
`@accounts_required @login_required @admin_required` (`app.py`), so it
403s anyone whose logged-in session email doesn't match `ADMIN_EMAIL`
(defaults to `jarededberg@gmail.com`, override via env var), 401s anyone
not logged in at all, and 503s if this deployment has no `DATABASE_URL`
configured — same three-state pattern as saved searches/applied jobs.
There's no separate admin password to manage; log in on the homepage with
the admin account, then visit `/admin`.

`db_users.get_user_stats(days=60)` (in `db_users.py`) does the actual
querying: a total count, signups in the last 7 days, the first-ever
signup's timestamp, and a daily series built with `generate_series()` +
`LEFT JOIN` rather than a plain `GROUP BY` — days with zero signups still
come back as explicit `{"day": ..., "count": 0}` entries instead of just
being absent, since a gap in the array reads as "no data" to Chart.js
(which `admin.html` loads from cdnjs), not "zero." The chart's date range
is adjustable (30/60/90/365 days) via a `?days=` query param on the same
endpoint, clamped server-side to 7–365 to keep the query bounded.

## FAQ / About

Both static content, no backend involved beyond the page routes
themselves (`/faq` → `static/faq.html`, `/about` → `static/about.html`,
both `send_from_directory` calls in `app.py`). FAQ's questions/answers
are written directly from this README's own documented behavior
(salary/YOE data being best-effort parses, match tiers being a keyword
heuristic, resumes not being stored server-side, department cohort-tag
cleanup, etc.) — if any of that behavior changes, update
`static/faq.html` to match. About is a short first-person bio/why-this-
exists page; edit `static/about.html` directly to update it. All three
content pages (About/FAQ/Contact) share the same nav (`.nav-links`) and
`.content-page`/`.page-form` CSS rules in `static/style.css`.

## Hunter (AI search assistant — no LLM, no external API, always on)

A single combined pill button at the top of the search panel/dashboard
(`#ai-search-btn`/`.ai-search-pill` in `static/index.html`), which opens
Hunter — a free-text chat interface — inside the same modal system used
elsewhere. **For future maintainers**: Hunter is a client-side rule-based
parser (regex/keyword extraction, `hunterParseMessage()` in `app.js`), not
an LLM. There's no API key, no cost, no network call beyond this app's own
`/api/jobs`/`/api/parse-resume`, and nothing to configure, so unlike every
other feature in this README there's no env var gating it on/off. The
site's own copy (FAQ, About) doesn't spell out "it's just a parser" the
way it briefly did in an earlier version — Hunter also won't affirmatively
claim to be a specific real AI model if asked; see `HUNTER_IDENTITY_RE`/
`HUNTER_IDENTITY_DEFLECTIONS` below. Keep that distinction in mind before
changing either the code or the site copy.

**Flow**: the person types a full sentence — anything from "remote senior
product manager job paying 150k+" to a bare "product manager" — and
`hunterParseMessage()` extracts whatever it recognizes:

- **Salary** — `$150k`, `150,000`, a bare 6-digit number, or "six
  figures" all resolve to a minimum. A currency word/symbol (USD/EUR/GBP/
  CAD/$/€/£/C$) is cosmetic only, same caveat as the old wizard: the
  dataset has no per-posting currency field (`salary_min`/`salary_max` in
  `db.py` are plain numbers, effectively USD), so it just changes the
  symbol shown, not what the number is compared against.
- **Years of experience** — "entry level"/"new grad" → 0, "junior" → 1,
  "mid-level" → 3, "senior" → 6, "staff"/"principal" → 10, or an explicit
  "5 years"/"5+ yrs".
- **Department** — `HUNTER_DEPT_KEYWORDS` maps phrases ("product
  manager", "swe", "account executive", "customer success", …) to the
  same ~14 canonical labels `department_groups.py` classifies raw scraped
  values into (see "Department cleanup" below), so a chat mention and a
  real facet value can never drift apart. Unlike the other filters,
  department mentions are detected but *not* stripped out of the message
  — "product manager" is both a department signal and useful title text
  for the keyword search itself.
- **Commitment** — "full-time"/"part-time"/"contract"/"intern(ship)"
  fuzzy-matched against whatever commitment values actually exist in the
  live `#commitment` `<select>`, same live-data principle the old wizard
  used.
- **Location** — checked in this order, each layer catching whatever the
  one before it couldn't: (1) "remote" → the canonical Remote (US) group,
  or any other `location_groups.py` group whose label appears in the
  message; (2) "City, ST"/"City, State" (`US_STATE_MAP` in `app.js`, all
  50 states + DC, abbreviation or full name, case-insensitive — "portland,
  or" and "Portland, Oregon" both resolve to "Portland, OR"); (3) a bare
  major-metro name with **no state at all** — "san francisco", "austin",
  "new york" — resolved against `metroCityMap`/`metroCityRe`
  (`loadMetroCities()` in `app.js`, fetched once from `GET
  /api/metro-cities`, which is just the ~68 curated `(city, state)` keys
  already in `metro_areas.py` — the same list the resume-upload flow uses
  server-side to expand a home city into nearby suburbs, reused here so
  there's one source of truth for "what counts as a known city" instead
  of a second hardcoded list drifting out of sync); (4) explicit "in
  `<city>`"/"near `<city>`"/"based in `<city>`"/"city is `<city>`"
  phrasing (case-insensitive, guarded by `HUNTER_STOPWORDS` so "in
  engineering"/"in sales" don't get mistaken for a city) as a last-resort
  fallback for real locations outside the curated metro list. **Fixed two
  real bugs here**: the location patterns originally required Title-Case
  input (`[A-Z][a-zA-Z]+`) on the theory that a real city name would be
  capitalized — but most people type lowercase in a chat box, so "in
  portland, ore[gon]", "portland, or", and "the city is portland, oregon"
  all silently failed to parse. And before the metro-city layer existed,
  a bare "san francisco" (no state, no trigger word) didn't match
  anything at all — a user has to know to type "in San Francisco" or
  "San Francisco, CA" for the *fallback* patterns to catch it, which
  isn't how people actually talk about where they live. The state-name
  whitelist gate (`US_STATE_MAP`) is what makes the "City, ST" pattern
  safe to run case-insensitively without also matching random commas
  elsewhere in a sentence; `metroCityRe` is one big case-insensitive
  alternation of every known city name, sorted longest-first so "san
  francisco" matches as a whole rather than partially. Locations apply
  live to the real `selectedLocations` array/chips as they're recognized,
  same as
  the old wizard's location step.
- **Recency** — "today"/"last 24 hours" → 1 day, "this week"/"past week"
  → 7, "last 2 weeks" → 14, "this month" → 30, etc.

Whatever's left over after stripping the recognized salary/YOE/
commitment/recency/location phrases from the *first* substantive message
becomes the search query (`hunterState.queryCaptured` flips to `true`
right after) — every message after that only ever affects the real
filters, never the query text again. **This was a real bug, not just a
design choice**: an earlier version kept merging every later message's
leftover text into the query too, on the theory that "product manager"
followed by "actually make it remote" shouldn't lose the original title.
In practice, ordinary chat sentences ("I live in Phoenix but would also
be open to remote roles") and questions directed at Hunter itself ("what
else should I enter?") aren't title text, and their filler words ended up
glued onto the search box and sent straight to the boolean search —
reliably returning zero results. A message ending in `?` (or matching
`HUNTER_HELP_RE`, common phrasings like "what should I type") is now
detected as a question and answered directly (`HUNTER_HELP_REPLY`)
instead of being parsed for query text at all. If a message is the resume
-upload path instead of typed text, `queryCaptured` is set immediately
(the query stays empty; matching comes from the resume's own extracted
terms, same as before), so nothing typed afterward can retroactively
rewrite it either.

Saying something like "search now", "run it", "go ahead", or "that's all"
(`HUNTER_FINALIZE_RE`) applies everything collected in `hunterState` to
the real filter controls (`hunterApplyToPage()`) and calls `search()` —
same role `wizardFinish()` played in the old click-through version — then
closes the modal after a short pause so the closing message is still
visible. Saying "restart"/"start over"/"reset" (`HUNTER_RESTART_RE`)
clears the transcript and state and starts over. Attaching a resume (the
paperclip icon next to the input — the one thing free text can't do on
its own) goes through the existing `handleResumeFile()`/
`/api/parse-resume` path unchanged, same as the main dropzone.

Hunter also handles the small talk around the actual filter-gathering —
a real user reported it felt too limited ("make sure he can actually
hold a conversation in a limited capacity"), since a message that carried
no recognizable filter used to fall straight through to the generic
"Didn't catch a specific filter there" reply even for things like "hi" or
"thanks". Now, whenever a message parses to zero filter bits
(`hunterRecapBits().length === 0`), it's checked against four more
patterns before giving up: `HUNTER_GREETING_RE` ("hi"/"hey"/"hello"/etc.,
answered from `HUNTER_GREETING_REPLIES`), `HUNTER_SMALLTALK_RE` ("how are
you"/"what's up", from `HUNTER_SMALLTALK_REPLIES`), `HUNTER_THANKS_RE`
("thanks"/"appreciate it", from `HUNTER_THANKS_REPLIES`), and
`HUNTER_STATUS_RE` ("what have I got so far"/"summarize", answered by
`hunterStatusSummary()`, which reads the live `hunterState` and
`selectedLocations` and recaps everything gathered in one plain-English
sentence — query, salary, YOE, departments, commitment, locations,
recency — so the person doesn't have to scroll back up to remember what
they've already told it). Each reply is picked at random from its pool
(`pickOne()`) so the conversation doesn't feel scripted on repeat. On top
of that, `HUNTER_FINALIZE_RE` was extended to also match a bare
affirmation on its own line — "yes"/"yeah"/"sure"/"ok"/"go for it" — since
Hunter always closes its own turn by asking "want to add more, or search
now?", so a one-word "yes" in that context means finalize, not an
unparseable non-answer.

A short "Hunter is typing…" delay (`hunterReply()`, scaled loosely to
reply length) precedes every response — purely cosmetic, the parsing
itself is synchronous — and reply wording is picked from small template
pools (`pickOne()`) rather than a single fixed string per situation, so
consecutive runs don't read as identically scripted.

**Department cleanup**: raw scraped `department` values are all over the
place — "Engineering", "Software Engineering", "AI Research &
Engineering", "GTM", "20213 S&M - Sales - Square Outside" — because
Greenhouse/Lever/Ashby just store whatever free-text label or internal
org-chart code a company typed into its ATS, and a picker built straight
off `SELECT DISTINCT department` read as unrecognizable next to the clean
department dropdowns on LinkedIn/Indeed. `department_groups.py`
classifies each raw string into one of ~14 canonical, familiar labels
(Engineering, Product, Design, Sales, Marketing, Customer Success,
Operations, Data, IT, Finance, People, Legal, Professional Services,
Executive) via ordered keyword matching — same classifier-over-raw-string
approach `location_groups.py` already uses for locations, including the
same reasoning for why it can't be a lookup table (no way to enumerate
every company's spelling in advance). Order matters: e.g. "Solutions
Engineering" is checked against Sales before the broad Engineering bucket
would otherwise swallow it (that's how it reads on every major job
board's own function taxonomy despite containing "engineering"), and IT
is checked before Engineering so "Information Technology" doesn't match
Engineering's broader "technology" keyword. Cohort/program-tag junk values
(e.g. `"EMEA '24"`, an intern/grad program class year, not a real
department — flagged by a real user as confusing) are filtered out
entirely via `_JUNK_DEPARTMENT_RE` (an apostrophe followed by a 2-digit
year) before classification, in `db._raw_department_values()`. Real
departments that don't match any canonical keyword set fall into a
synthetic "Other" bucket rather than disappearing.

This classification happens at query time against the raw `department`
column (never stored/migrated), same as location grouping: `GET
/api/facets` returns canonical labels (`db.department_group_facets()`),
and filtering happens entirely in Python, post-fetch (`_dept_matches()`
inside `db.search_jobs()`) rather than as a SQL `WHERE` clause — an
earlier version expanded each requested canonical label to every matching
raw value and OR'd them as SQL bound parameters, which crashed the whole
site in production once real data had enough distinct raw department
strings to exceed SQLite's ~999-bound-parameter limit
(`sqlite3.OperationalError: too many SQL variables`, hit on essentially
every request). See the comments at the top of `db.search_jobs()` and
`db._raw_department_values()` for the full incident writeup — the
takeaway for any future filter that expands a category to "every raw
value that matches" is: filter in Python after the fetch, never build a
SQL parameter list from live data. A raw value that doesn't classify into
any recognized label (an old saved search storing a value from before
this grouping existed) falls back to literal matching, so nothing needs
migrating. Department is
multi-select everywhere it appears: `db.search_jobs(departments=[...])`
and `GET /api/jobs?department=a&department=b` (repeated param, same
convention as `location`); the main page's department picker
(`#department-select` in `static/index.html`) is a custom checkbox
dropdown (`renderDepartmentMenu()` in `app.js`) rather than a native
`<select multiple>`, since a native multi-select renders as an
always-open listbox and needs ctrl/cmd-click most people don't know
about.

**Pagination**: in addition to Prev/Next and numbered page buttons,
`renderResults()` appends a `<select class="page-jump">` with one option
per page whenever there's more than one page — handy for jumping straight
to, say, page 40 of results without clicking Next 39 times.

**Other search-panel additions**: the posted-date dropdown (`#days`) adds
"Last 24 hours" (`value="1"`) and "Last 3 days" (`value="3"`) ahead of the
existing 7/14/30/90-day options — `db.search_jobs`'s day-cutoff math
already handled any integer, so this was purely an `index.html` addition.
"Clear search" (`#clear-search-btn`, `clearSearch()` in `app.js`) resets
every filter — query text, days, department/commitment, sort, location
chips, both range sliders, and any uploaded resume's extracted terms —
back to the as-loaded default and re-runs an unfiltered search, without
touching saved searches or account state. Originally styled with the same
generic `.row-action-btn` look as everything else in that row (1px muted
border, small text) — a real user testing the page reported not being
able to find it, so `.clear-search-btn` now has its own rule in
`style.css`: a 2px accent border and a tinted background so it reads as a
real control rather than blending into the filter row, same "decorative
vs. clickable" issue the resume dropzone had before its own restyle
below. "Home" is the first item in
`.nav-links` on every page (index/About/FAQ/Contact), linking back to
`/`. The resume dropzone (`.resume-dropzone`) was restyled solid maroon
and enlarged — the original dashed-border box read as decorative rather
than clickable to a real user testing it.

**Implementation notes**: `openHunterModal()` (near the end of
`static/app.js`) builds the modal content and re-queries
`assistantMessages`/`assistantForm`/`assistantInput`/`hunterResumeBtn`/
`hunterResumeInput` each time it opens, since `openModal()` replaces
`#modal-content`'s `innerHTML` on every call. `hunterState` (a small state
object, reset by `resetHunterState()`) accumulates whatever's parsed out
across turns; `hunterHandleMessage()` is the per-message driver —
identity-question check → restart check → `hunterParseMessage()` →
`hunterApplyParsed()` (pushes locations/departments live) → either
`hunterApplyToPage()` (on a finalize phrase) or a template-pool reply
acknowledging what was just added. `hunterApplyToPage()` reuses the exact
same filter-application patterns `loadSavedSearch()` already uses for
saved searches. There's no button-driven step state anymore — the whole
thing is one flat message handler, which is simpler than the old wizard's
per-step function chain but means `hunterParseMessage()` is the one
function that has to stay correct; see its inline comments for why
department mentions aren't stripped from the leftover query text the way
every other recognized phrase is.

## Branding

The hero in `static/index.html` (`.hero-badges`, `.hero h1`, `.tagline`)
leads with two things: this only indexes Greenhouse/Lever/Ashby career
pages — by construction, never Workday, iCIMS, or Oracle, which is the
actual differentiator worth calling out (those platforms are usually the
annoying multi-page-application, create-another-account experience
LinkedIn/Indeed results route through) — and the "AI-powered" framing for
Hunter. The footer includes a short "why this exists"
blurb and a LinkedIn link; the fuller version of that story lives on
`/about` (`static/about.html`). Edit these directly to change the
framing, or swap the LinkedIn/GitHub URLs.
