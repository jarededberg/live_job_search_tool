"""
db.py — tiny SQLite layer for the job cache.
"""

import hashlib
import re
import sqlite3
import os
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from boolean_search import parse_query, evaluate, leaf_terms
from location_groups import matches_group, is_clearly_non_us, is_remote, city_state_variants
from department_groups import classify_department, DEPARTMENT_DISPLAY_ORDER
from blurb_extractor import parse_years_range
from role_groups import classify_role, ROLE_DISPLAY_ORDER

DB_PATH = os.environ.get("JOBS_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.db"))
MAX_CANDIDATES = 50000  # cap on rows pulled from SQLite before Python-side boolean evaluation

_lock = threading.Lock()


def compute_job_id(url):
    """A short, stable, URL-safe identifier for a job, derived from its
    own URL (the table's actual primary key) rather than a fresh
    autoincrement id -- this table never had one, and adding one now
    would mean every existing row gets renumbered on the next deploy,
    which would break any /jobs/<id> links already shared or indexed by
    a search engine by the time this ships. Deriving it from the URL
    instead means the same job always gets the same id, forever, without
    needing a migration to assign one. 12 hex chars (48 bits) is
    overwhelmingly enough to avoid collisions at this dataset's size
    (~100k rows) -- full SHA-256 would just make the URL uglier for no
    practical benefit here. Used for the public /jobs/<job_id>-<slug>
    detail page route (see app.py) and its sitemap."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


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
        if "job_id" not in cols:
            # Backing column for the public /jobs/<job_id>-<slug> detail
            # page (see app.py + compute_job_id() above). Added via ALTER
            # rather than being part of the original CREATE TABLE since
            # this ships after the table already has real production
            # data; the backfill loop right below assigns every existing
            # row its (deterministic) id in one pass so this only ever
            # runs once per deployment, not on every startup.
            conn.execute("ALTER TABLE jobs ADD COLUMN job_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_department ON jobs(department)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_commitment ON jobs(commitment)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_job_id ON jobs(job_id)")
        # Composite index backing similar_jobs()'s "same department,
        # newest first" lookup on job detail pages -- one of the
        # highest-traffic page types on the site (see the salary-stats
        # incident in the README for why every query that can run on a
        # page like this needs to be provably indexed, not just fast in
        # a small test). Without the `posted` column included here,
        # SQLite could use idx_jobs_department for the equality filter
        # but would still need a separate sort step over every matching
        # row before it could apply LIMIT; with it, the index itself is
        # already in the right order.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_department_posted ON jobs(department, posted)")

        # One-time backfill: any row with no job_id yet (freshly-added
        # column, or a row upserted by an older deploy of scraper.py
        # before this existed) gets one computed now. Cheap even at
        # ~100k rows -- this is pure Python hashing, no network calls --
        # and upsert_jobs() keeps every row populated going forward, so
        # this loop naturally does less work (ideally nothing) on every
        # startup after the first.
        missing = conn.execute("SELECT url FROM jobs WHERE job_id IS NULL").fetchall()
        if missing:
            conn.executemany(
                "UPDATE jobs SET job_id = ? WHERE url = ?",
                [(compute_job_id(row["url"]), row["url"]) for row in missing],
            )
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
        run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scrape_runs)").fetchall()}
        if "companies_not_found" not in run_cols:
            conn.execute("ALTER TABLE scrape_runs ADD COLUMN companies_not_found INTEGER")
        if "companies_found_no_openings" not in run_cols:
            conn.execute("ALTER TABLE scrape_runs ADD COLUMN companies_found_no_openings INTEGER")

        # platform_cache: remembers which ATS (Greenhouse/Lever/Ashby) each
        # slug last resolved on, so scrape_all() can try that platform first
        # on the next run instead of always trying all three in order for
        # every one of ~4,300 companies. Self-healing: if a cached platform
        # ever stops resolving (company migrated ATS providers), scraper.py
        # falls back to the full three-platform search and this table just
        # gets overwritten with whatever it finds instead. Not part of the
        # original CREATE TABLE since this ships after companies_data.py
        # already has thousands of real entries with no recorded platform;
        # it starts empty and fills in gradually, one scrape cycle at a time.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS platform_cache (
                slug TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                updated_at TEXT
            )
        """)

        # company_requests: the public "Request a company" form. Lives in
        # this SQLite db (not the Postgres accounts db) on purpose -- this
        # needs to work for anonymous visitors on a deployment with no
        # accounts/DATABASE_URL configured at all, same reasoning as the
        # job cache itself being the "no account needed" default. Low
        # volume, not sensitive, no real need for the durability Postgres
        # gives the accounts data.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                careers_url TEXT DEFAULT '',
                requester_email TEXT DEFAULT '',
                note TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_company_requests_status ON company_requests(status)")

        # job_flags: the "report a problem with this listing" button on
        # job detail pages. Same reasoning as company_requests for living
        # here rather than the Postgres accounts db (has to work with no
        # account/DATABASE_URL at all) -- and same reasoning for storing
        # job_url/job_title/company as plain text snapshots rather than a
        # foreign key into `jobs`: a flagged listing (e.g. "this is
        # closed") is exactly the kind of row likely to get pruned by
        # prune_stale() before anyone reviews the flag, so the report
        # needs to stand on its own rather than going dead the moment the
        # underlying job row disappears.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_url TEXT NOT NULL,
                job_title TEXT DEFAULT '',
                company TEXT DEFAULT '',
                reason TEXT NOT NULL,
                note TEXT DEFAULT '',
                reporter_email TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_flags_status ON job_flags(status)")

        # digest_subscribers: single-opt-in email list for the weekly
        # "best remote roles" digest (/weekly-digest). Lives here (SQLite,
        # no account needed) rather than the Postgres accounts db for the
        # same reason as company_requests/job_flags -- this is meant to be
        # a zero-friction "just give an email" signup, not gated behind
        # creating a full account.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS digest_subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                unsubscribed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS digest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TEXT NOT NULL,
                subscribers_emailed INTEGER
            )
        """)

        # mcp_saved_searches: the MCP server's save_search/create_job_alert
        # tools (see mcp_server.py) -- an email-based equivalent of
        # db_users.py's saved_searches+alerts_enabled for callers with no
        # login at all (an AI agent acting on someone's behalf over MCP
        # has no session cookie to attach a real account to). Lives here
        # in the no-account SQLite db for the same reason company_requests/
        # job_flags/digest_subscribers do -- discrete filter columns
        # rather than one JSON blob (unlike db_users.saved_searches' own
        # params_json) since every filter an MCP tool call can express
        # maps onto one of db.search_jobs()'s own keyword arguments
        # one-to-one, with no legacy singular/plural shape to stay
        # backward-compatible with the way the browser UI's params blob
        # does.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mcp_saved_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                location_group TEXT NOT NULL DEFAULT '',
                department TEXT NOT NULL DEFAULT '',
                commitment TEXT NOT NULL DEFAULT '',
                alerts_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_checked_at TEXT,
                alert_frequency TEXT NOT NULL DEFAULT 'daily'
            )
        """)
        mcp_cols = {row["name"] for row in conn.execute("PRAGMA table_info(mcp_saved_searches)").fetchall()}
        if "alert_frequency" not in mcp_cols:
            conn.execute("ALTER TABLE mcp_saved_searches ADD COLUMN alert_frequency TEXT NOT NULL DEFAULT 'daily'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_saved_searches_email ON mcp_saved_searches(email)")

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
            job_id = compute_job_id(j["url"])
            cur = conn.execute("SELECT url FROM jobs WHERE url = ?", (j["url"],))
            if cur.fetchone() is None:
                new_count += 1
                conn.execute(
                    "INSERT INTO jobs (url, title, company, location, posted, source, department, "
                    "commitment, salary_min, salary_max, blurb, years_experience, tools, "
                    "first_seen, last_seen, job_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (j["url"], j["title"], j["company"], j["location"], j["posted"], j["source"],
                     dept, commit_, salary_min, salary_max, blurb, years_experience, tools, now, now,
                     job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET title=?, company=?, location=?, posted=?, source=?, department=?, "
                    "commitment=?, salary_min=?, salary_max=?, blurb=?, years_experience=?, tools=?, "
                    "last_seen=?, job_id=? WHERE url=?",
                    (j["title"], j["company"], j["location"], j["posted"], j["source"], dept, commit_,
                     salary_min, salary_max, blurb, years_experience, tools, now, job_id, j["url"]),
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
            "companies_with_jobs, jobs_scraped, jobs_in_db_after, companies_not_found, "
            "companies_found_no_openings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (started_at, finished_at, status, stats.get("companies_scanned", 0),
             stats.get("companies_with_jobs", 0), stats.get("jobs_scraped", 0), jobs_in_db_after,
             stats.get("companies_not_found", 0), stats.get("companies_found_no_openings", 0)),
        )
        conn.commit()


def last_run():
    with conn_ctx() as conn:
        row = conn.execute("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def recent_runs(limit=10):
    """Most recent scrape runs, newest first -- used for the health check
    (comparing the latest run's companies_not_found against recent history
    to flag a spike) and for showing an admin a quick trend at a glance."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def total_jobs():
    with conn_ctx() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]


def get_platform_cache():
    """Returns {slug: platform} for every company whose resolving ATS
    platform is already known from a previous scrape. Empty on a fresh
    database -- scraper.py falls back to trying all three platforms for
    any slug missing from this dict, so an empty/partial cache is always
    safe, just slower until it fills in."""
    with conn_ctx() as conn:
        rows = conn.execute("SELECT slug, platform FROM platform_cache").fetchall()
        return {r["slug"]: r["platform"] for r in rows}


def upsert_platform_cache(mapping):
    """mapping: {slug: platform}. Bulk upsert, called once at the end of a
    scrape with every slug that resolved during that run (whether from a
    cache hit or a fresh three-platform search)."""
    if not mapping:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _lock, conn_ctx() as conn:
        conn.executemany(
            "INSERT INTO platform_cache (slug, platform, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET platform=excluded.platform, updated_at=excluded.updated_at",
            [(slug, platform, now) for slug, platform in mapping.items()],
        )
        conn.commit()


def create_company_request(company_name, careers_url="", requester_email="", note=""):
    now = datetime.now(timezone.utc).isoformat()
    with _lock, conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO company_requests (company_name, careers_url, requester_email, note, "
            "status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (company_name, careers_url, requester_email, note, now),
        )
        conn.commit()
        return cur.lastrowid


def list_company_requests(status=None, limit=200):
    """All submitted company requests, newest first. Filtered to a single
    status (e.g. 'pending') if given, otherwise everything -- powers the
    admin dashboard's review list."""
    with conn_ctx() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM company_requests WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM company_requests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def update_company_request_status(request_id, status):
    """Returns True if a row was actually updated. `status` isn't
    validated against a fixed set here -- app.py's route does that, same
    division of responsibility as APPLICATION_STATUSES in db_users.py."""
    with _lock, conn_ctx() as conn:
        cur = conn.execute(
            "UPDATE company_requests SET status = ? WHERE id = ?", (status, request_id)
        )
        conn.commit()
        return cur.rowcount > 0


def create_job_flag(job_url, job_title, company, reason, note="", reporter_email=""):
    now = datetime.now(timezone.utc).isoformat()
    with _lock, conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO job_flags (job_url, job_title, company, reason, note, reporter_email, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (job_url, job_title, company, reason, note, reporter_email, now),
        )
        conn.commit()
        return cur.lastrowid


def list_job_flags(status=None, limit=200):
    with conn_ctx() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM job_flags WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM job_flags ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def update_job_flag_status(flag_id, status):
    with _lock, conn_ctx() as conn:
        cur = conn.execute(
            "UPDATE job_flags SET status = ? WHERE id = ?", (status, flag_id)
        )
        conn.commit()
        return cur.rowcount > 0


def subscribe_to_digest(email):
    """Idempotent: re-subscribing an already-active email is a silent
    no-op (UNIQUE(email) with INSERT OR IGNORE), and re-subscribing a
    previously-unsubscribed email clears unsubscribed_at rather than
    erroring, so clicking an old "subscribe" link/bookmark twice or
    resubscribing after opting out both just work."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock, conn_ctx() as conn:
        conn.execute(
            "INSERT INTO digest_subscribers (email, created_at) VALUES (?, ?) "
            "ON CONFLICT(email) DO UPDATE SET unsubscribed_at = NULL",
            (email, now),
        )
        conn.commit()


def unsubscribe_from_digest(email):
    now = datetime.now(timezone.utc).isoformat()
    with _lock, conn_ctx() as conn:
        cur = conn.execute(
            "UPDATE digest_subscribers SET unsubscribed_at = ? WHERE email = ? AND unsubscribed_at IS NULL",
            (now, email),
        )
        conn.commit()
        return cur.rowcount > 0


def list_active_digest_subscribers():
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT email FROM digest_subscribers WHERE unsubscribed_at IS NULL"
        ).fetchall()
        return [r["email"] for r in rows]


def record_digest_run(ran_at, subscribers_emailed):
    with conn_ctx() as conn:
        conn.execute(
            "INSERT INTO digest_runs (ran_at, subscribers_emailed) VALUES (?, ?)",
            (ran_at, subscribers_emailed),
        )
        conn.commit()


def last_digest_run():
    with conn_ctx() as conn:
        row = conn.execute("SELECT * FROM digest_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def create_mcp_saved_search(email, name="", query="", location="", location_group="",
                             department="", commitment="", alerts_enabled=False,
                             alert_frequency="daily"):
    """Creates one row for mcp_server.py's save_search/create_job_alert
    tools. `alerts_enabled` is the only difference between the two tools
    at the storage layer -- save_search inserts with it False (a pure
    bookmark), create_job_alert inserts with it True (also gets picked up
    by the scheduled alert job, see app.py's run_mcp_search_alerts_job()).
    `alert_frequency` ('daily' or 'weekly') is only meaningful when
    alerts_enabled is True -- see app.py's _alert_is_due(). Returns the
    new row's id."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock, conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO mcp_saved_searches (email, name, query, location, location_group, "
            "department, commitment, alerts_enabled, created_at, alert_frequency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, name, query, location, location_group, department, commitment,
             1 if alerts_enabled else 0, now, alert_frequency),
        )
        conn.commit()
        return cur.lastrowid


def list_mcp_saved_searches_for_email(email):
    """Every saved search/alert belonging to one email, newest first --
    powers the list_my_searches MCP tool. Ownership here is just "knows
    the email" (no password, no login) -- same trust model as the
    unsubscribe-by-email-plus-token links elsewhere on this site, but
    even lighter since there's no destructive action gated behind it
    (worst case someone who doesn't own the address turns email alerts
    on/off for a search that's about as sensitive as a Google Alert)."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_saved_searches WHERE email = ? ORDER BY id DESC", (email,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_mcp_saved_search(search_id):
    with conn_ctx() as conn:
        row = conn.execute("SELECT * FROM mcp_saved_searches WHERE id = ?", (search_id,)).fetchone()
        return dict(row) if row else None


def set_mcp_search_alerts(search_id, email, enabled):
    """Flips alerts_enabled for one row, scoped to `email` matching what's
    on file for that id -- powers cancel_job_alert (and could re-enable
    one later). Returns True only if a row was actually found AND owned
    by that email, so a wrong/mistyped email can't silently no-op look
    like success to the caller."""
    with _lock, conn_ctx() as conn:
        cur = conn.execute(
            "UPDATE mcp_saved_searches SET alerts_enabled = ? WHERE id = ? AND email = ?",
            (1 if enabled else 0, search_id, email),
        )
        conn.commit()
        return cur.rowcount > 0


def list_mcp_searches_for_alerts():
    """Every row with alerts_enabled -- the MCP-alert equivalent of
    db_users.list_searches_for_alerts(), consumed by app.py's
    run_mcp_search_alerts_job()."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_saved_searches WHERE alerts_enabled = 1"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_mcp_search_checked(search_id, checked_at):
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE mcp_saved_searches SET last_checked_at = ? WHERE id = ?",
            (checked_at.isoformat() if hasattr(checked_at, "isoformat") else checked_at, search_id),
        )
        conn.commit()


def list_mcp_saved_searches(limit=200):
    """Every MCP-created saved search/alert across ALL emails, newest
    first -- unlike list_mcp_saved_searches_for_email() above (which is
    scoped to one caller-supplied email, the MCP tools' own trust model),
    this is for the admin dashboard's "MCP saved searches" table, where
    the whole point is visibility across every agent that's used the
    remote MCP server. `limit` is a sane ceiling for a page meant to be
    skimmed by a human, not a full export -- same reasoning
    jobs_for_company()'s own `limit` docstring gives."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_saved_searches ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


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


def _apply_filter_cap(tier, score, unconfirmed):
    """If a job's salary/YOE data couldn't be confirmed against an active
    range filter (see search_jobs' `_unconfirmed_filter` marking, set on
    the salary and YOE filter blocks above), it's still shown -- not
    excluded -- but its tier is capped at "good": we can't verify it
    actually satisfies a range the candidate deliberately set, so it
    shouldn't outrank a job that provably does. Only ever lowers "best"
    to "good" -- never touches "good" or "poor" (nothing to cap), and
    never applies to a job that isn't flagged unconfirmed at all. The
    1000-point score deduction mirrors _match_info's own tier_weight*1000
    term, so a capped job's score lands in the same range a naturally
    "good"-tier job's would, keeping match-sort order consistent."""
    if unconfirmed and tier == "best":
        return "good", score - 1000
    return tier, score


def search_jobs(query="", location="", locations=None, location_groups=None, days=None,
                 department="", departments=None, commitment="", sort=DEFAULT_SORT,
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

    `departments`, if given, is a list of canonical department labels
    (see department_groups.py — "Engineering", "Sales", etc; multi-select,
    same OR-across-the-list convention as `locations`) — a job matches if
    its raw scraped department classifies into ANY of them. `department`
    (singular) is kept for backward compatibility as a single-value
    filter; if both are given, `departments` wins. A value that isn't a
    recognized canonical label is matched literally against the raw
    department column instead (keeps old saved searches, from before this
    grouping existed, working unchanged). Matched entirely in Python
    against already-fetched candidate rows (classify_department() called
    per-row), NOT as a SQL WHERE clause — an earlier version expanded each
    requested label into every matching raw department string and bound
    them all as SQL parameters, which is exactly what crashed production:
    a popular bucket like "Engineering" expands to far more than SQLite's
    default ~999-parameter limit once there are thousands of companies'
    worth of raw spelling variants in the real dataset. Keep department
    filtering in Python going forward for this reason.

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
    common case: most searches never touch the sliders). A job whose
    salary/YOE data is present and CONFIRMED to fall outside the
    requested range is excluded -- that's a real, known mismatch. A job
    with no salary/YOE data at all is a different case and is no longer
    excluded: there's nothing to confirm it doesn't fit, so removing it
    outright would be throwing away real listings over a data gap, not a
    real mismatch. It's kept in results but flagged internally
    (`_unconfirmed_filter`, stripped before the row leaves this function)
    so `_match_info`'s tier for that job gets capped at "good" rather
    than "best" when a resume is uploaded -- unconfirmed against a filter
    the candidate deliberately set shouldn't outrank a job that provably
    satisfies it, but it also shouldn't just vanish. (No resume uploaded
    at all means no tiers exist regardless, so an unconfirmed job just
    appears like any other result, no special treatment.) This used to
    hard-exclude missing data entirely, mirroring how most job boards
    handle salary filters -- changed per explicit user feedback that a
    job shouldn't disappear just because a field failed to parse.
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

    # Department filtering happens entirely in Python, post-fetch (see
    # `_dept_matches` below) rather than as a SQL WHERE clause -- an
    # earlier version expanded each requested canonical label ("Engineering")
    # to every matching raw department string and OR'd them as SQL
    # parameters, which crashed the whole site in production
    # ("too many SQL variables") the moment a popular canonical bucket
    # expanded to more raw spellings than SQLite's default ~999 bound-
    # parameter limit across a real ~4,300-company dataset -- something a
    # small local test dataset never had enough distinct raw values to
    # surface. Filtering candidates in Python after they're already
    # fetched (same approach `group_list` location matching below uses)
    # sidesteps the whole class of bug: no SQL parameter count that scales
    # with how many raw spellings happen to exist.
    dept_list = [d.strip() for d in (departments or []) if d and d.strip()]
    if not dept_list and department.strip():
        dept_list = [department.strip()]

    if commitment.strip():
        where.append("commitment = ?")
        params.append(commitment.strip())

    salary_filter_active = salary_min is not None or salary_max is not None
    if salary_filter_active:
        # Range-overlap test against the real salary_min/salary_max
        # columns: a job passes if its own posted range overlaps the
        # requested [lo, hi] at all, not just if it's fully contained --
        # e.g. a job posted as "$140k-$180k" should still show up for a
        # "$150k+" filter even though $140k is below the floor. Missing
        # `salary_min`/`salary_max` on either side falls back to
        # unbounded (0 / a very large number) rather than narrowing that
        # side at all.
        #
        # A job with NO salary data at all also passes here (the
        # `salary_min IS NULL` branch) rather than being excluded -- see
        # this function's docstring. It gets flagged as unconfirmed
        # further down, once `candidates` exists as real Python dicts, so
        # its match tier (if any) can be capped instead of the row being
        # dropped entirely.
        lo = salary_min if salary_min is not None else 0
        hi = salary_max if salary_max is not None else 10**9
        where.append(
            "(salary_min IS NULL OR salary_max IS NULL "
            "OR (salary_max >= ? AND salary_min <= ?))"
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

    if salary_filter_active:
        # The SQL above already let missing-salary rows through instead of
        # excluding them -- this just marks which ones so the match-tier
        # step below (if a resume is uploaded) knows to cap them at "good"
        # rather than treat them as a confirmed fit. See search_jobs'
        # docstring.
        for r in candidates:
            if not r.get("salary_min") or not r.get("salary_max"):
                r["_unconfirmed_filter"] = True

    if dept_list:
        # Canonical labels ("Engineering", "Sales", ..., plus the
        # synthetic "Other" bucket) are matched via classify_department();
        # anything in dept_list that ISN'T a recognized canonical label is
        # an old saved search storing a raw scraped department string from
        # before this grouping existed, and gets matched literally instead
        # -- no migration needed for those to keep working.
        canonical_wanted = {d for d in dept_list if d in DEPARTMENT_DISPLAY_ORDER or d == "Other"}
        literal_wanted = {d for d in dept_list if d not in DEPARTMENT_DISPLAY_ORDER and d != "Other"}

        def _dept_matches(r):
            raw = r.get("department") or ""
            if raw in literal_wanted:
                return True
            if not canonical_wanted:
                return False
            if _JUNK_DEPARTMENT_RE.search(raw):
                return False  # cohort/program tags never count toward any bucket, including "Other"
            label = classify_department(raw)
            return (label in canonical_wanted) if label else ("Other" in canonical_wanted)

        candidates = [r for r in candidates if _dept_matches(r)]

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

        def _yoe_status(r):
            """'mismatch' (confirmed outside the range -- exclude),
            'unconfirmed' (no parseable YOE at all -- keep, but flag for
            the match-tier cap below), or 'match'."""
            job_lo, job_hi = parse_years_range(r.get("years_experience"))
            if job_lo is None:
                return "unconfirmed"
            if job_hi is None:
                # Open-ended "X+" from the posting -- treat it as
                # satisfying any requested ceiling rather than excluding
                # a "10+" job just because the filter's ceiling is 12.
                job_hi = 999
            return "match" if (job_hi >= yoe_lo and job_lo <= yoe_hi) else "mismatch"

        kept = []
        for r in candidates:
            status = _yoe_status(r)
            if status == "mismatch":
                continue  # confirmed doesn't fit -- a real reason to exclude
            if status == "unconfirmed":
                r["_unconfirmed_filter"] = True
            kept.append(r)
        candidates = kept

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
            tier, score = _apply_filter_cap(tier, score, r.get("_unconfirmed_filter", False))
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
            tier, _score = _apply_filter_cap(tier, _score, r.get("_unconfirmed_filter", False))
            r["match_tier"] = tier

    for r in page_rows:
        r.pop("_match_score", None)  # internal sort key, not part of the API response
        r.pop("_unconfirmed_filter", None)  # internal tier-cap flag, not part of the API response
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


def get_job_by_job_id(job_id):
    """Single job row for the public /jobs/<job_id>-<slug> detail page
    (see app.py). Returns None if not found -- either job_id is garbage
    (someone hand-typed a URL) or, far more commonly, it's a real id for
    a posting that's since closed and dropped out of the live dataset
    (see prune_stale); the route treats both the same way, a real 404,
    not a soft-404 200, so search engines actually deindex the page
    instead of leaving a permanently-stale result in results pages.
    LIMIT 1 rather than assuming uniqueness -- job_id is a 12-hex-char
    hash (see compute_job_id()), so a collision is astronomically
    unlikely at this dataset's size but not provably impossible, and
    silently returning one arbitrary match beats a crash if it ever
    happens."""
    with conn_ctx() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ? LIMIT 1", (job_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["tools"] = json.loads(d.get("tools") or "[]")
    except (TypeError, ValueError):
        d["tools"] = []
    return d


def list_jobs_for_sitemap(offset, limit):
    """A stable page of (job_id, url, company, title, last_seen) tuples
    for the dynamic jobs sitemap (see app.py's /sitemap-jobs-<n>.xml).
    Ordered by url -- the table's real primary key -- purely so repeated
    calls with the same offset/limit return the same page every time
    (SQLite doesn't guarantee row order without an explicit ORDER BY,
    and a sitemap that reshuffles which jobs land on which page between
    requests would make search engines re-crawl pages that haven't
    actually changed, and risk skipping ones that have)."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT job_id, url, company, title, last_seen FROM jobs "
            "ORDER BY url LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def list_companies_with_open_jobs():
    """(company, count) for every company that currently has at least one
    live job -- used to build the /jobs/company/<slug> hub pages and
    their sitemap (see app.py). Deliberately only companies with a
    current opening: a hub page for a company with zero live roles would
    just be a dead end, exactly the kind of thin/empty page that drags
    down how a search engine weighs the rest of the site's content."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT company, COUNT(*) AS c FROM jobs GROUP BY company ORDER BY company"
        ).fetchall()
    return [{"company": r["company"], "count": r["c"]} for r in rows]


def jobs_for_company(company, limit=300):
    """Every current job at one company, newest first -- the listing
    body of a /jobs/company/<slug> hub page. `limit` is a sane ceiling
    (a handful of companies post hundreds of roles at once); this is a
    page meant to be skimmed and clicked through to individual job
    pages, not a full paginated search in itself -- that's what the
    homepage search is for."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE company = ? ORDER BY posted DESC LIMIT ?",
            (company, limit),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["tools"] = json.loads(d.get("tools") or "[]")
        except (TypeError, ValueError):
            d["tools"] = []
        result.append(d)
    return result


def list_newest_jobs(limit=50):
    """The `limit` most-recently-posted live jobs site-wide, newest
    first -- powers /feed.xml (see app.py's newest_jobs_feed()). A plain
    `ORDER BY posted DESC LIMIT ?` with no WHERE clause, so it's served
    entirely off the existing idx_jobs_posted index as an index scan in
    reverse with no sort step and no full-table read -- same reasoning
    as similar_jobs()'s composite index below, just without needing an
    equality filter first. `limit` is capped small (an RSS reader has no
    interest in thousands of items) rather than exposed as a paginated
    feed."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT job_id, company, title, location, posted, last_seen "
            "FROM jobs ORDER BY posted DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


_SIMILAR_JOBS_CANDIDATE_POOL = 200  # see similar_jobs()'s performance note


def similar_jobs(job, limit=5):
    """A handful of OTHER companies' current postings similar to `job` --
    same canonical role (role_groups.classify_role) preferred, falling
    back to same raw department otherwise. Powers the "More jobs like
    this" block on job detail pages (see render_job_page() in app.py).
    Deliberately excludes `job`'s own company entirely -- the existing
    "More at this company" block already covers that, so this is meant
    to complement it, not duplicate it.

    Performance note (see the salary-stats incident in this README):
    job detail pages are some of the highest-traffic pages on the whole
    site, so this can NEVER become an unbounded full-table scan. The
    query below filters on the indexed `department` column (backed by
    idx_jobs_department_posted, a composite index that also covers the
    ORDER BY so SQLite doesn't need a separate sort step) and caps the
    candidate set at _SIMILAR_JOBS_CANDIDATE_POOL BEFORE any Python-side
    role classification runs -- classify_role() (a regex match) only
    ever gets called on that bounded set, never on every job in the
    department, let alone the whole table. Worst case, on a department
    with more than the candidate-pool cap worth of live postings, this
    can miss a same-role match further down the list than the pool
    reaches -- an acceptable tradeoff for a "you might also like this"
    block, which doesn't need to be exhaustive, over ever risking an
    unbounded per-request scan on this page type again."""
    department = job.get("department") or ""
    company = job.get("company")
    job_id = job.get("job_id")
    if not department:
        return []
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE department = ? AND company != ? "
            "ORDER BY posted DESC LIMIT ?",
            (department, company, _SIMILAR_JOBS_CANDIDATE_POOL),
        ).fetchall()
    candidates = [dict(r) for r in rows if r["job_id"] != job_id]

    target_role = classify_role(job.get("title") or "")
    same_role, other_dept = [], []
    for c in candidates:
        if target_role and classify_role(c["title"]) == target_role:
            same_role.append(c)
        else:
            other_dept.append(c)
    result = (same_role + other_dept)[:limit]
    for d in result:
        try:
            d["tools"] = json.loads(d.get("tools") or "[]")
        except (TypeError, ValueError):
            d["tools"] = []
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


def _median(sorted_values):
    """Standard median of an already-sorted list -- average of the two
    middle values on an even count, the single middle value on odd.
    Separate tiny helper (rather than inlining) because both
    salary_stats_by_role() and salary_stats_by_company() need the exact
    same definition, and a stats page showing a different median
    convention between its two tables would look like a bug."""
    n = len(sorted_values)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return sorted_values[mid]
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2


MIN_SALARY_SAMPLE_SIZE = 3  # see salary_stats_by_role()/salary_stats_by_company() docstrings


def _salary_rollup_rows():
    """Every (title, company, salary_min, salary_max) tuple for jobs that
    report BOTH salary bounds -- the shared raw material for both
    role-level and company-level salary rollups below. One query, reused
    by both, so the two pages can never drift out of sync about which
    underlying jobs counted."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT title, company, salary_min, salary_max FROM jobs "
            "WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL"
        ).fetchall()
    return rows


def _rollup_stats(midpoints, lo_values, hi_values):
    """Shared aggregate shape for one bucket (one role or one company):
    sample size, median of each job's own (min+max)/2 midpoint (a single
    number per posting is more meaningful to show as "the" salary for a
    bucket than separately medianing mins and maxes against each other),
    and the overall low/high range actually posted anywhere in the
    bucket."""
    midpoints_sorted = sorted(midpoints)
    return {
        "count": len(midpoints),
        "median": _median(midpoints_sorted),
        "low": min(lo_values),
        "high": max(hi_values),
    }


def salary_stats_by_role():
    """One row per canonical role (see role_groups.py) that has at least
    MIN_SALARY_SAMPLE_SIZE salary-confirmed postings currently in the
    dataset -- {"label", "slug", "count", "median", "low", "high"},
    ordered by ROLE_DISPLAY_ORDER. Powers the /salary index page. The
    minimum-sample-size cutoff exists so a role with just one or two
    confirmed postings (which could be a single outlier company's pay
    band) doesn't get presented as if it were a reliable market figure --
    same "don't overclaim confidence a small sample can't support"
    reasoning as showing a real 404 rather than a thin/misleading page
    elsewhere on this site."""
    rows = _salary_rollup_rows()
    buckets = {}
    for r in rows:
        label = classify_role(r["title"])
        if label is None:
            continue
        buckets.setdefault(label, {"mid": [], "lo": [], "hi": []})
        buckets[label]["mid"].append((r["salary_min"] + r["salary_max"]) / 2)
        buckets[label]["lo"].append(r["salary_min"])
        buckets[label]["hi"].append(r["salary_max"])

    results = []
    for label in ROLE_DISPLAY_ORDER:
        b = buckets.get(label)
        if not b or len(b["mid"]) < MIN_SALARY_SAMPLE_SIZE:
            continue
        stats = _rollup_stats(b["mid"], b["lo"], b["hi"])
        stats["label"] = label
        results.append(stats)
    return results


def salary_stats_for_role(role_label):
    """Same aggregate shape as one entry of salary_stats_by_role(), for a
    single canonical role -- returns None if that role has fewer than
    MIN_SALARY_SAMPLE_SIZE confirmed postings right now (caller should
    404, same convention as _find_company_by_slug()'s no-current-openings
    case)."""
    rows = _salary_rollup_rows()
    mid, lo, hi = [], [], []
    for r in rows:
        if classify_role(r["title"]) == role_label:
            mid.append((r["salary_min"] + r["salary_max"]) / 2)
            lo.append(r["salary_min"])
            hi.append(r["salary_max"])
    if len(mid) < MIN_SALARY_SAMPLE_SIZE:
        return None
    return _rollup_stats(mid, lo, hi)


def jobs_for_role(role_label, limit=100):
    """Current salary-confirmed postings that classify into one canonical
    role, highest-salary first -- the listing body of a /salary/<slug>
    page (people land there to see what roles like this actually pay, so
    leading with the highest-paying real postings is the most useful
    order, same idea as the weekly digest's salary_high sort)."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL"
        ).fetchall()
    matched = [dict(r) for r in rows if classify_role(r["title"]) == role_label]
    matched.sort(key=lambda r: r.get("salary_max") or 0, reverse=True)
    result = []
    for d in matched[:limit]:
        try:
            d["tools"] = json.loads(d.get("tools") or "[]")
        except (TypeError, ValueError):
            d["tools"] = []
        result.append(d)
    return result


def salary_stats_by_company(min_sample=MIN_SALARY_SAMPLE_SIZE, limit=500):
    """One row per company with at least `min_sample` salary-confirmed
    postings -- {"company", "count", "median", "low", "high"}, sorted by
    median descending (highest-paying companies first, the natural way
    someone browsing a "salary insights" index would want this ranked).
    Same minimum-sample-size reasoning as salary_stats_by_role(): a
    company with one or two disclosed salaries shouldn't be presented
    next to companies with dozens, ranked as if they were equally
    reliable figures."""
    rows = _salary_rollup_rows()
    buckets = {}
    for r in rows:
        buckets.setdefault(r["company"], {"mid": [], "lo": [], "hi": []})
        buckets[r["company"]]["mid"].append((r["salary_min"] + r["salary_max"]) / 2)
        buckets[r["company"]]["lo"].append(r["salary_min"])
        buckets[r["company"]]["hi"].append(r["salary_max"])

    results = []
    for company, b in buckets.items():
        if len(b["mid"]) < min_sample:
            continue
        stats = _rollup_stats(b["mid"], b["lo"], b["hi"])
        stats["company"] = company
        results.append(stats)
    results.sort(key=lambda s: s["median"], reverse=True)
    return results[:limit]


def salary_stats_for_company(company):
    """Same aggregate shape as one entry of salary_stats_by_company(), for
    a single company -- returns None if that company has fewer than
    MIN_SALARY_SAMPLE_SIZE confirmed postings right now."""
    rows = _salary_rollup_rows()
    mid, lo, hi = [], [], []
    for r in rows:
        if r["company"] == company:
            mid.append((r["salary_min"] + r["salary_max"]) / 2)
            lo.append(r["salary_min"])
            hi.append(r["salary_max"])
    if len(mid) < MIN_SALARY_SAMPLE_SIZE:
        return None
    return _rollup_stats(mid, lo, hi)


def salary_confirmed_jobs_by_role(limit_per_role=50):
    """Every canonical role's current highest-paying disclosed-salary
    postings, computed in ONE pass over the jobs table -- {label: [job,
    ...]}. This replaces calling jobs_for_role() once per role (22
    separate full-table scans, one per canonical role) with a single
    scan that buckets every row by its classified role as it goes.

    This function exists specifically because of a real production
    outage: the original /salary/<role> route called db.jobs_for_role()
    (its own full "SELECT * FROM jobs WHERE salary_min/max IS NOT NULL"
    scan plus a classify_role() call per row) directly, on every single
    page view, with no caching -- fine against the tiny fake datasets
    used while building the feature, but a full unindexed scan against
    a real ~100k-row production table is expensive enough that a single
    slow request could tie up the app's one worker process and time out
    every other visitor behind it (a Cloudflare 524 on the ENTIRE site,
    not just /salary). The fix on the app.py side is an in-memory cache
    with a TTL that calls this function once per refresh instead of once
    per request; this function's job is just to make one refresh cost
    one scan instead of 22."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL"
        ).fetchall()
    buckets = {}
    for r in rows:
        label = classify_role(r["title"])
        if label is None:
            continue
        buckets.setdefault(label, []).append(dict(r))
    result = {}
    for label, jobs in buckets.items():
        jobs.sort(key=lambda j: j.get("salary_max") or 0, reverse=True)
        trimmed = jobs[:limit_per_role]
        for d in trimmed:
            try:
                d["tools"] = json.loads(d.get("tools") or "[]")
            except (TypeError, ValueError):
                d["tools"] = []
        result[label] = trimmed
    return result


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


# Some scraped "department" values are cohort/program tags, not real
# departments -- e.g. "EMEA '24"/"EMEA '25" (an intern/grad program class
# year, not a department a candidate would ever filter by), flagged by a
# real user as confusing to see in the department picker. Matched by an
# apostrophe immediately followed by a 2-digit year rather than a fixed
# list of company-specific program names, since new ones show up as more
# companies get scraped and a hardcoded blocklist would just keep missing
# the next one.
_JUNK_DEPARTMENT_RE = re.compile(r"'\d{2}\b")


def _raw_department_values():
    """Every distinct non-empty raw `department` string currently in the
    DB, with the junk cohort/program-tag values (see _JUNK_DEPARTMENT_RE)
    already excluded -- used by department_group_facets() to build the
    picker's option list. (Filtering itself -- matching a job against a
    requested canonical label -- happens per-candidate in Python inside
    search_jobs(), via classify_department() directly on each row's own
    department value, not by pre-computing this list and expanding it into
    SQL parameters -- see search_jobs()'s docstring/comments for why that
    approach was actually tried first and had to be reverted.)"""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT DISTINCT department AS v FROM jobs WHERE department != ''"
        ).fetchall()
    return [row["v"] for row in rows if row["v"] and not _JUNK_DEPARTMENT_RE.search(row["v"])]


def department_group_facets(limit=30):
    """Canonical department labels (see department_groups.py) that at
    least one current job classifies into, ordered by DEPARTMENT_DISPLAY_ORDER
    (a fixed "reads like a normal job board" order, not raw frequency, so
    the picker doesn't reshuffle every time the underlying data changes)
    with an "Other" bucket appended last if any real (non-junk) department
    value didn't classify into anything."""
    raws = _raw_department_values()
    present = set()
    has_other = False
    for raw in raws:
        label = classify_department(raw)
        if label:
            present.add(label)
        else:
            has_other = True
    ordered = [label for label in DEPARTMENT_DISPLAY_ORDER if label in present]
    if has_other:
        ordered.append("Other")
    return ordered[:limit]


def distinct_facet_values(column, limit=30):
    """Top N non-empty distinct values for a facet column, ordered by
    frequency. `department` is special-cased to return canonical grouped
    labels (see department_group_facets()) instead of raw scraped
    strings -- see department_groups.py for why."""
    assert column in ("department", "commitment")
    if column == "department":
        return department_group_facets(limit)
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
