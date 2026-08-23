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
from location_groups import matches_group

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


def _match_info(job, title_terms, skill_terms):
    """Classify a job against resume-derived terms (both already lowercased)
    into a ("best"/"good"/"poor"/None) tier plus a sortable ordinal score
    (higher = better match). Returns (None, 0) if no resume terms were
    supplied at all (no resume uploaded).

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
    entries, not extraction bugs.)"""
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
                 resume_title_terms=None, resume_skill_terms=None,
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
    for r in candidates:
        # stored as a JSON string (SQLite has no native array type); parse
        # back to a real list for the API response. Malformed/legacy rows
        # (pre-migration, or NULL) fall back to an empty list rather than
        # raising.
        try:
            r["tools"] = json.loads(r.get("tools") or "[]")
        except (TypeError, ValueError):
            r["tools"] = []
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

    title_terms = [t.lower() for t in (resume_title_terms or []) if t]
    skill_terms = [t.lower() for t in (resume_skill_terms or []) if t]
    has_resume_terms = bool(title_terms or skill_terms)

    if has_resume_terms:
        # Attach match_tier to every candidate (not just when sort=="match")
        # since the frontend badges whatever's on the current page
        # regardless of sort order — the sort below only additionally
        # reorders using the same numbers.
        for r in candidates:
            tier, score = _match_info(r, title_terms, skill_terms)
            r["match_tier"] = tier
            r["_match_score"] = score

    if sort == "match" and has_resume_terms:
        # Two stable sorts = one two-key sort: establish newest-first as the
        # tiebreaker order first, then sort by score — Python's sort is
        # stable, so equal-score jobs keep their relative newest-first order
        # from the first pass. This has to happen here (in Python, over the
        # full candidate set) rather than in the browser, because the
        # browser only ever sees one page at a time — it can badge the jobs
        # it can see, but it can't reorder page 3 relative to page 1.
        candidates.sort(key=lambda r: r.get("posted") or "", reverse=True)
        candidates.sort(key=lambda r: -r["_match_score"])

    total = len(candidates)
    offset = max(0, (page - 1)) * per_page
    page_rows = candidates[offset: offset + per_page]
    for r in page_rows:
        r.pop("_match_score", None)  # internal sort key, not part of the API response
    return page_rows, total


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
