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

### Location-aware match tiers

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
since they might still be US-eligible; `is_clearly_non_us()`'s docstring
also notes a known gap (no APAC/LatAm classifier yet, so an unlabeled
"Remote - APAC" wouldn't be caught). If the resume parser couldn't
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
