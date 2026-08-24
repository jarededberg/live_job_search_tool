"""
db.py — tiny SQLite layer for the job cache.
"""

import sqlite3
import os
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from boolean_search import parse_query, evaluate, leaf_terms
from location_groups import matches_group, is_clearly_non_us, is_remote, city_state_variants
from blurb_extractor import parse_years_range

DB_PATH = os.environ.get("JOBS_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.db"))
MAX_CANDIDATES = 50000  # cap on rows pulled from SQLite before Python-side boolean evaluation

_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


@contextmanager
def conn_ctx():
    """Like get_conn(), but actually closes the connection afterward.

    `with sqlite3.connect(...) as conn:` only commits/rolls back on exit —
    it does NOT close the connection. Every function below used to rely on
    that pattern, which meant every call (hundreds of times per scrape, via
    upsert_jobs) leaked an open connection that never got closed. Those piled
    up holding locks on the WAL file, which is what caused "database is
    locked" errors on ordinary reads while a scrape was running. This wraps
    get_conn() so callers get commit-on-success/rollback-on-error AND a
    guaranteed close.
    """
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with conn_ctx() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                posted TEXT,
                source TEXT,
                department TEXT DEFAULT '',
                commitment TEXT DEFAULT '',
                salary_min INTEGER,
                salary_max INTEGER,
                blurb TEXT DEFAULT '',
                years_experience TEXT,
                tools TEXT DEFAULT '[]',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        # Lightweight migration for DBs created before department/commitment/salary/blurb existed
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "department" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN department TEXT DEFAULT ''")
        if "commitment" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN commitment TEXT DEFAULT ''")
        if "salary_min" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN salary_min INTEGER")
        if "salary_max" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN salary_max INTEGER")
        if "blurb" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN blurb TEXT DEFAULT ''")
        if "years_experience" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN years_experience TEXT")
        if "tools" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN tools TEXT DEFAULT '[]'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_department ON jobs(department)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_commitment ON jobs(commitment)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scrape_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                finished_at TEXT,
                status TEXT,
                companies_scanned INTEGER,
                companies_with_jobs INTEGER,
                jobs_scraped INTEGER,
                jobs_in_db_after INTEGER
            )
        """)
        conn.commit()


def upsert_jobs(jobs):
    """Insert new jobs / refresh last_seen for jobs still open. Returns count of brand-new rows."""
    now = datetime.now(timezone.utc).isoformat()
    new_count = 0
    with _lock, conn_ctx() as conn:
        for j in jobs:
            dept = j.get("department", "")
            commit_ = j.get("commitment", "")
            salary_min = j.get("salary_min")
            salary_max = j.get("salary_max")
            blurb = j.get("blurb", "")
            years_experience = j.get("years_experience")
            tools = json.dumps(j.get("tools") or [])
            cur = conn.execute("SELECT url FROM jobs WHERE url = ?", (j["url"],))
            if cur.fetchone() is None:
                new_count += 1
                conn.execute(
                    "INSERT INTO jobs (url, title, company, location, posted, source, department, "
                    "commitment, salary_min, salary_max, blurb, years_experience, tools, "
                    "first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (j["url"], j["title"], j["company"], j["location"], j["posted"], j["source"],
                     dept, commit_, salary_min, salary_max, blurb, years_experience, tools, now, now),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET title=?, company=?, location=?, posted=?, source=?, department=?, "
                    "commitment=?, salary_min=?, salary_max=?, blurb=?, years_experience=?, tools=?, "
                    "last_seen=? WHERE url=?",
                    (j["title"], j["company"], j["location"], j["posted"], j["source"], dept, commit_,
                     salary_min, salary_max, blurb, years_experience, tools, now, j["url"]),
                )
        conn.commit()
    return new_count


def prune_stale(max_age_days=10):
    """Drop jobs not seen in the last N scrape cycles (i.e. likely closed/filled)."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with _lock, conn_ctx() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE last_seen < ?", (cutoff,))
        conn.commit()
        return cur.rowcount


def record_run(started_at, finished_at, status, stats, jobs_in_db_after):
    with conn_ctx() as conn:
        conn.execute(
            "INSERT INTO scrape_runs (started_at, finished_at, status, companies_scanned, "
            "companies_with_jobs, jobs_scraped, jobs_in_db_after) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (started_at, finished_at, status, stats.get("companies_scanned", 0),
             stats.get("companies_with_jobs", 0), stats.get("jobs_scraped", 0), jobs_in_db_after),
        )
        conn.commit()


def last_run():
    with conn_ctx() as conn:
        row = conn.execute("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def total_jobs():
    with conn_ctx() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]


SORT_OPTIONS = {
    "newest": "(posted IS NULL), posted DESC, company ASC",
    "oldest": "(posted IS NULL), posted ASC, company ASC",
    "company": "company ASC, (posted IS NULL), posted DESC",
    "salary_high": "(salary_max IS NULL), salary_max DESC, (posted IS NULL), posted DESC",
    "salary_low": "(salary_min IS NULL), salary_min ASC, (posted IS NULL), posted DESC",
    # "match" isn't a SQL ORDER BY — it's computed in Python below, since it
    # depends on resume terms that only exist per-request. Left out of this
    # dict on purpose so SORT_OPTIONS.get(sort, ...) falls back to "newest"
    # for the underlying SQL fetch, which the Python-side match sort then
    # overrides entirely.
}
DEFAULT_SORT = "newest"


_GENERIC_TERMS = {"operations", "ops", "strategy", "management"}


def _is_location_excluded(job, us_based, metro_terms):
    """True if `job` should be excluded from results entirely because its
    location doesn't work for the candidate: not viable for a US-based
    candidate at all (location_groups.is_clearly_non_us), or -- when
    `metro_terms` is given -- an onsite (non-remote) role outside the
    candidate's home metro. Same classification `_match_info` originally
    used to badge a job "poor" with; pulled out into its own function so
    `search_jobs()` can use it as a hard filter instead.

    This used to only ever demote a tier, never remove a result — visible
    but buried felt like the safer default after an earlier bug where
    auto-applying a location filter on resume upload collapsed a 600-job
    search down to 7 (see "Resume upload showing too few total results" in
    the README). But real usage showed the opposite complaint: users
    wanted onsite roles they can't actually take (wrong city, wrong
    country) out of the list entirely, not just pushed to the bottom.
    de-duplicated from `_match_info` so the exact same rule governs both
    places it's used to matter (this filter, and match-tier scoring below
    for whatever's left after the filter runs)."""
    if not us_based:
        return False
    loc = job.get("location") or ""
    if is_clearly_non_us(loc):
        return True
    if metro_terms and loc and not is_remote(loc):
        loc_lower = loc.lower()
        if not any(term in loc_lower for term in metro_terms):
            return True
    return False


def _match_info(job, title_terms, skill_terms):
    """Classify a job against resume-derived terms (both already lowercased)
    into a ("best"/"good"/"poor"/None) tier plus a sortable ordinal score
    (higher = better match). Returns (None, 0) if no resume terms were
    supplied at all (no resume uploaded).

    Location used to be checked in here too (a job whose location didn't
    work for the candidate got force-set to "poor" regardless of title/
    skill overlap). It's been pulled out into `_is_location_excluded()`
    and is now applied as an actual filter in `search_jobs()`, BEFORE this
    function ever runs — per explicit user choice, a job the candidate
    can't take shouldn't just be buried at the bottom of the list, it
    shouldn't be in the results at all. So by the time a job reaches here,
    its location has already been judged fine (or location wasn't a
    factor because we never confidently placed the candidate). "poor" from
    this function now means "title/skill overlap was weak," never
    "location was wrong."

    This replaced an earlier version that scored `matched_terms /
    len(all_terms)` against title+blurb only, which produced far too few
    "good"/"best" matches in practice for two compounding reasons:

    1. `terms` used to mix title-derived words with generic skill/tool names
       (Salesforce, SQL, Excel, ...) in one shared list. Tool names almost
       never appear in a job title or in the ~1-2 sentence qualifications
       blurb scraped from a posting, so they mostly just inflated the
       denominator — a genuinely on-target job still scored low because
       most of the (very long) term list could never realistically match.
    2. The haystack was only title + blurb. A job's `department` field
       (e.g. "Revenue Operations", "Sales", "Engineering") is a short,
       structured ATS label that's often a much stronger and more reliable
       signal than free-text blurb content, and wasn't being used at all.

    Now: title-derived terms (`title_terms` — the extracted title(s) PLUS
    role-synonym expansions from role_synonyms.py) are checked against the
    title directly for the strongest tier, and title/skill terms are
    checked against blurb+department as a secondary signal — direct hit
    counts, not a ratio, so a long tail of extra synonym terms only ever
    helps, never dilutes.

    One deliberate carve-out: a handful of synonym groups in
    role_synonyms.py include a bare single-word "related" term ("Operations",
    "Ops", "Strategy", "Management") as a catch-all. Those are too generic
    to trust as a standalone "best match" signal on their own — a
    warehouse/logistics job's `department` is very often literally
    "Operations" too, and a huge fraction of ALL job titles contain
    "Manager" somewhere. So `_GENERIC_TERMS` never single-handedly produce
    "best": they're split out from the specific title terms and only count
    toward the weaker "good" tier and the tiebreak score, never the primary
    title-hit check. (Resume-extracted title PHRASES themselves are always
    2+ words by the time they reach here — see resume_parser.py's
    ROLE_NOUN_RE and the word-count check in _extract_title_phrases — so
    this carve-out is specifically about role_synonyms.py's bare catch-all
    entries, not extraction bugs.)

    (See `_is_location_excluded()` above for the location rule itself and
    the history of why it moved from a demotion to a hard filter.)"""
    if not title_terms and not skill_terms:
        return None, 0
    title_lower = (job.get("title") or "").lower()
    body = f"{(job.get('blurb') or '').lower()} {(job.get('department') or '').lower()}"

    specific_terms = [t for t in title_terms if t not in _GENERIC_TERMS]
    generic_terms = [t for t in title_terms if t in _GENERIC_TERMS]

    title_hits = sum(1 for t in specific_terms if t in title_lower)
    generic_title_hits = sum(1 for t in generic_terms if t in title_lower)
    body_title_hits = sum(1 for t in specific_terms if t in body)
    skill_hits = sum(1 for t in skill_terms if t in body or t in title_lower)

    if title_hits > 0:
        tier = "best"
    elif body_title_hits > 0 or generic_title_hits > 0 or skill_hits >= 2:
        tier = "good"
    else:
        tier = "poor"

    tier_weight = {"best": 2, "good": 1, "poor": 0}[tier]
    score = (tier_weight * 1000 + title_hits * 50 + body_title_hits * 10
             + generic_title_hits * 5 + skill_hits)
    return tier, score


def search_jobs(query="", location="", locations=None, location_groups=None, days=None,
                 department="", commitment="", sort=DEFAULT_SORT,
                 resume_title_terms=None, resume_skill_terms=None, resume_us_based=False,
                 resume_metro_terms=None,
                 salary_min=None, salary_max=None, yoe_min=None, yoe_max=None,
                 page=1, per_page=25):
    """Boolean keyword search against title (AND/OR/NOT/quotes/parens via
    boolean_search.py), plus facet filters for location substring, days-back,
    department, and commitment. Boolean evaluation happens in Python; SQL is
    only used to (a) apply the facet filters and (b) cheaply narrow the
    candidate set using a safe superset (OR of all non-negated terms) before
    evaluating the full expression.

    `locations`, if given, is a list of substrings (multi-select) — a job
    matches if its location contains ANY of them. `location` (singular) is
    kept for backward compatibility as a single substring filter; if both
    are given, `locations` wins.

    `location_groups`, if given, is a list of canonical group keys from
    location_groups.py (e.g. "remote_us") — a job matches if its raw
    location string satisfies that group's classifier. This collapses the
    dozens of raw spellings ATSes use for "remote in the US" into one
    selectable option. Combined with `locations`/`location` via OR, same as
    multi-select location chips are combined with each other.

    `resume_title_terms` / `resume_skill_terms` (lists of lowercased
    strings, from resume_parser.suggest_query) drive per-job match-tier
    classification (see `_match_info`). Every returned job gets a
    `match_tier` field (None if neither list is given) — this happens
    regardless of `sort`, since the frontend badges every visible card, not
    just when sorted by match. `sort="match"` (best match first, ties
    broken by newest-first) additionally reorders the full candidate set by
    that tier/score before pagination — something the browser can't do
    itself since it only ever sees one page at a time. With no resume terms,
    `sort="match"` quietly falls back to newest-first rather than raising,
    since both only ever come from query params a user could hand-edit.

    `resume_us_based`, if True, means the candidate's resume was
    successfully parsed to a US city — this removes any job whose location
    doesn't work for them from the results ENTIRELY (not just a "poor"
    badge): a bare foreign city with no US signal ("Prague",
    "Peterborough"), an explicitly non-US remote label ("Remote (Canada)"/
    "(UK)"/"(Europe)"/LatAm), or (when `resume_metro_terms` is also given)
    an onsite role outside the candidate's home metro. See
    `_is_location_excluded()` for the exact rule. False (the default, and
    what's sent whenever the resume parser couldn't confidently place the
    candidate in the US) leaves location out of filtering entirely, same
    as before this param existed — nothing gets removed on account of
    location unless we're actually confident where the candidate is.

    This used to only demote a job's match tier to "poor" rather than
    remove it, on the theory that a still-visible-but-buried job preserves
    optionality (maybe the candidate would relocate, maybe a "remote"
    label is just poorly written). Changed to an outright filter per
    explicit user feedback: they wanted roles they can't actually take out
    of the list, not just sorted to the bottom of it. Worth remembering if
    this behavior ever gets revisited — the earlier "too few results" bug
    (see the README) is exactly the failure mode to watch for if location
    filtering like this ever gets layered together with other automatic
    narrowing again.

    `resume_metro_terms`: lowercased "city, st" substrings for the
    candidate's home metro area (resume_parser.extract_location's
    nearby-metro list). Only takes effect when `resume_us_based` is also
    True; ignored otherwise.

    `salary_min`/`salary_max` and `yoe_min`/`yoe_max` are range-slider
    filters (see the frontend's dual-thumb sliders, sized against
    `salary_bounds()`/`years_bounds()`). Each pair is independently
    optional -- `None` on one side means "no ceiling"/"no floor" on that
    side, and `None` on both means the filter isn't active at all (the
    common case: most searches never touch the sliders). A job only
    passes a filter that IS active if it actually reports the relevant
    data AND that data's own range overlaps the requested range --
    salary via the real `salary_min`/`salary_max` columns in SQL, YOE via
    parsing the free-text `years_experience` column in Python (same
    `parse_years_range()` used to compute `years_bounds()`). Jobs with no
    salary/YOE data are excluded once the corresponding filter is active,
    on the theory that a candidate who deliberately narrowed the range
    wants confirmed matches, not unknowns padding the count -- this
    mirrors how most job boards handle salary filters, and is a
    deliberate departure from the "benefit of the doubt" treatment given
    to missing location data elsewhere in this file.
    """
    ast = parse_query(query)
    order_sql = SORT_OPTIONS.get(sort, SORT_OPTIONS[DEFAULT_SORT])

    where = []
    params = []

    loc_list = [l.strip() for l in (locations or []) if l and l.strip()]
    if not loc_list and location.strip():
        loc_list = [location.strip()]
    group_list = [g.strip() for g in (location_groups or []) if g and g.strip()]

    if loc_list or group_list:
        clauses = ["location LIKE ?" for _ in loc_list]
        params.extend(f"%{l}%" for l in loc_list)
        if group_list:
            # Cheap SQL-side narrowing only, not the real decision — every
            # canonical group is some flavor of remote, so this broadly
            # keeps candidate rows in play. The precise per-group
            # classification (matches_group) happens in Python below, once
            # rows are already fetched.
            clauses.append("location LIKE '%remote%'")
        # Jobs with no reported location still show up rather than getting
        # hidden by a filter they simply have no data to match against.
        where.append("(" + " OR ".join(clauses) + " OR location = '' OR location IS NULL)")

    if days:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%d")
        where.append("(posted >= ? OR posted IS NULL OR posted = '')")
        params.append(cutoff)

    if department.strip():
        where.append("department = ?")
        params.append(department.strip())

    if commitment.strip():
        where.append("commitment = ?")
        params.append(commitment.strip())

    if salary_min is not None or salary_max is not None:
        # Range-overlap test against the real salary_min/salary_max
        # columns: a job passes if its own posted range overlaps the
        # requested [lo, hi] at all, not just if it's fully contained --
        # e.g. a job posted as "$140k-$180k" should still show up for a
        # "$150k+" filter even though $140k is below the floor. Missing
        # `salary_min`/`salary_max` on either side falls back to
        # unbounded (0 / a very large number) rather than narrowing that
        # side at all.
        lo = salary_min if salary_min is not None else 0
        hi = salary_max if salary_max is not None else 10**9
        where.append(
            "(salary_min IS NOT NULL AND salary_max IS NOT NULL "
            "AND salary_max >= ? AND salary_min <= ?)"
        )
        params.append(lo)
        params.append(hi)

    positive_terms = leaf_terms(ast) if ast else []
    if positive_terms:
        clauses = ["title LIKE ?" for _ in positive_terms]
        where.append("(" + " OR ".join(clauses) + ")")
        params.extend(f"%{t}%" for t in positive_terms)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with conn_ctx() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs {where_sql} ORDER BY {order_sql} "
            f"LIMIT ?",
            params + [MAX_CANDIDATES],
        ).fetchall()

    candidates = [dict(r) for r in rows]
    # `tools` is stored as a JSON string (SQLite has no native array type)
    # and needs parsing back to a real list for the API response — but only
    # for rows that actually make it into the response. Deliberately NOT
    # done here for every candidate (same reasoning as the match-tier fix
    # below): parsing JSON on up to MAX_CANDIDATES rows when only
    # `per_page` of them ever get returned was pure wasted work on every
    # single request.
    if ast is not None:
        candidates = [r for r in candidates if evaluate(ast, r["title"])]

    if group_list:
        # The SQL prefilter above was deliberately loose (any "remote"
        # substring) when groups were requested — this is the actual
        # decision. Combined via OR with plain location substrings, same as
        # multi-select location chips are OR'd with each other.
        loc_list_lower = [l.lower() for l in loc_list]

        def _loc_matches(r):
            loc = r.get("location") or ""
            if not loc:
                return True
            if loc_list_lower and any(l in loc.lower() for l in loc_list_lower):
                return True
            return any(matches_group(g, loc) for g in group_list)

        candidates = [r for r in candidates if _loc_matches(r)]

    # Expand each "city, st" term to also cover the state spelled out in
    # full ("phoenix, az" -> also "phoenix, arizona") -- real scraped job
    # locations use both forms inconsistently, and metro_areas.py's
    # curated lists are all abbreviated. See city_state_variants()'s
    # docstring for the real bug this fixes.
    metro_terms = set()
    for t in (resume_metro_terms or []):
        if t:
            metro_terms |= city_state_variants(t)

    if resume_us_based:
        # Hard filter, not a badge demotion -- per explicit user choice, a
        # job whose location doesn't work for the candidate (not viable
        # for a US-based candidate at all, or an onsite role outside their
        # home metro) is removed from the results entirely rather than
        # kept around tagged "poor match." Runs whenever we're confident
        # about the candidate's location, independent of `sort` -- this
        # is a real filter on what's shown, not a scoring detail that only
        # matters for match-sorted results.
        candidates = [r for r in candidates if not _is_location_excluded(r, resume_us_based, metro_terms)]

    if yoe_min is not None or yoe_max is not None:
        # Same range-overlap idea as the salary filter above, but done in
        # Python post-fetch instead of SQL, since `years_experience` is
        # free text ("5+", "3-5") rather than a numeric column -- has to
        # be parsed per-row via parse_years_range() before it can be
        # compared to the requested range at all.
        yoe_lo = yoe_min if yoe_min is not None else 0
        yoe_hi = yoe_max if yoe_max is not None else 999

        def _yoe_overlaps(r):
            job_lo, job_hi = parse_years_range(r.get("years_experience"))
            if job_lo is None:
                return False  # can't confirm a fit -- exclude rather than
                               # guess, same call as the salary filter
            if job_hi is None:
                # Open-ended "X+" from the posting -- treat it as
                # satisfying any requested ceiling rather than excluding
                # a "10+" job just because the filter's ceiling is 12.
                job_hi = 999
            return job_hi >= yoe_lo and job_lo <= yoe_hi

        candidates = [r for r in candidates if _yoe_overlaps(r)]

    title_terms = [t.lower() for t in (resume_title_terms or []) if t]
    skill_terms = [t.lower() for t in (resume_skill_terms or []) if t]
    has_resume_terms = bool(title_terms or skill_terms)

    if has_resume_terms and sort == "match":
        # Scoring the FULL candidate set (could be tens of thousands of
        # rows) is only actually necessary here, when the sort order itself
        # depends on the score — the browser can't reorder page 3 relative
        # to page 1 on its own, so this has to happen before pagination.
        for r in candidates:
            tier, score = _match_info(r, title_terms, skill_terms)
            r["match_tier"] = tier
            r["_match_score"] = score
        # Two stable sorts = one two-key sort: establish newest-first as the
        # tiebreaker order first, then sort by score — Python's sort is
        # stable, so equal-score jobs keep their relative newest-first order
        # from the first pass.
        candidates.sort(key=lambda r: r.get("posted") or "", reverse=True)
        candidates.sort(key=lambda r: -r["_match_score"])

    total = len(candidates)
    offset = max(0, (page - 1)) * per_page
    page_rows = candidates[offset: offset + per_page]

    if has_resume_terms and sort != "match":
        # Any other sort order doesn't need the whole candidate set scored
        # — badges only need to exist for the ~25-50 rows actually being
        # returned this page, not however many thousand matched the
        # search. This used to run unconditionally on every candidate
        # regardless of sort, which was fine back when the location filter
        # was applied automatically on resume upload (shrinking the
        # candidate set way down) but turned into real, measurable latency
        # once that auto-filter was removed and searches started scoring
        # the full unfiltered dataset on every request.
        for r in page_rows:
            tier, _score = _match_info(r, title_terms, skill_terms)
            r["match_tier"] = tier

    for r in page_rows:
        r.pop("_match_score", None)  # internal sort key, not part of the API response
        try:
            r["tools"] = json.loads(r.get("tools") or "[]")
        except (TypeError, ValueError):
            r["tools"] = []
    return page_rows, total


def get_jobs_by_urls(urls):
    """Full job rows for a given list of URLs, keyed by URL — used to
    join Postgres's applied_jobs table (which only stores the URL, not a
    copy of the job's title/company/etc.) back to the live-scraped data
    for the "My Applications" view. A URL applied to weeks ago may no
    longer be in this table at all if the posting closed and dropped out
    of the live dataset (see prune_stale) — callers should expect fewer
    results back than URLs given in, not treat a missing one as an
    error, and still show *something* for it (the bare URL, no title/
    company) rather than silently hiding a real application record."""
    if not urls:
        return {}
    placeholders = ",".join("?" for _ in urls)
    with conn_ctx() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE url IN ({placeholders})", list(urls)
        ).fetchall()
    result = {}
    for r in rows:
        d = dict(r)
        try:
            d["tools"] = json.loads(d.get("tools") or "[]")
        except (TypeError, ValueError):
            d["tools"] = []
        result[d["url"]] = d
    return result


def salary_bounds():
    """(min, max) of salary_min/salary_max across every job that reports
    both -- used to size the salary range slider's endpoints so the
    handles' starting positions reflect what's actually in the dataset,
    not an arbitrary guess. Falls back to a reasonable default range when
    nothing in the DB has salary data yet (fresh install, first scrape
    still running)."""
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT MIN(salary_min) AS lo, MAX(salary_max) AS hi FROM jobs "
            "WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL"
        ).fetchone()
    if row["lo"] is None or row["hi"] is None:
        return 40000, 300000
    # Round outward to the nearest $5k so the slider's endpoints read as
    # clean numbers instead of whatever oddly-specific figure happened to
    # be the single lowest/highest posted salary in the dataset.
    lo = (row["lo"] // 5000) * 5000
    hi = -(-row["hi"] // 5000) * 5000  # ceiling division
    return int(lo), int(hi)


YOE_SLIDER_MAX = 20  # see years_bounds() docstring


def years_bounds():
    """(min, max) for the YOE slider's endpoints. Fixed at (0, 20) rather
    than derived from the data (that was the original design -- see git
    history) because a handful of postings parse to wildly unreasonable
    values (a "365" showed up in practice, almost certainly a mis-scraped
    "365 days" PTO figure rather than a real years-of-experience figure,
    not worth chasing down in blurb_extractor.py for one outlier). A fixed
    0-20 range with 20 meaning "20+" is both more predictable for users and
    immune to whatever the next weird outlier turns out to be.

    This only affects where the slider's handles start/end -- the actual
    filter semantics (search_jobs()'s `_yoe_overlaps`) already treat the
    slider's right handle sitting at its max as "no ceiling requested" (see
    app.js's search(), which only sends `yoe_max` once the handle has moved
    off the endpoint), so leaving the slider at its new 0-20 default still
    surfaces a genuinely-20+-years posting same as before -- nothing about
    real filtering changes, just what number the slider's ceiling reads as."""
    return 0, YOE_SLIDER_MAX


def distinct_facet_values(column, limit=30):
    """Top N non-empty distinct values for a facet column, ordered by frequency."""
    assert column in ("department", "commitment")
    with conn_ctx() as conn:
        rows = conn.execute(
            f"SELECT {column} AS v, COUNT(*) AS c FROM jobs WHERE {column} != '' "
            f"GROUP BY {column} ORDER BY c DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row["v"] for row in rows]


def distinct_locations(prefix="", limit=20):
    """Distinct non-empty location strings for the typeahead dropdown,
    ranked by how many open roles use them. `prefix` is actually matched as
    a substring anywhere in the location (not just a true prefix) since
    location strings vary a lot in format ("Remote - US", "US - Remote",
    "New York, NY"), and a strict prefix match would miss too much."""
    with conn_ctx() as conn:
        prefix = prefix.strip()
        if prefix:
            rows = conn.execute(
                "SELECT location AS v, COUNT(*) AS c FROM jobs WHERE location LIKE ? AND location != '' "
                "GROUP BY location ORDER BY c DESC LIMIT ?",
                (f"%{prefix}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT location AS v, COUNT(*) AS c FROM jobs WHERE location != '' "
                "GROUP BY location ORDER BY c DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row["v"] for row in rows]
