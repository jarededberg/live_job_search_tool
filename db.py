"""
db.py — tiny SQLite layer for the job cache.
"""

import sqlite3
import os
import threading
from datetime import datetime, timezone

from boolean_search import parse_query, evaluate, leaf_terms

DB_PATH = os.environ.get("JOBS_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.db"))
MAX_CANDIDATES = 50000  # cap on rows pulled from SQLite before Python-side boolean evaluation

_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with get_conn() as conn:
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
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        # Lightweight migration for DBs created before department/commitment existed
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "department" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN department TEXT DEFAULT ''")
        if "commitment" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN commitment TEXT DEFAULT ''")
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
    with _lock, get_conn() as conn:
        for j in jobs:
            dept = j.get("department", "")
            commit_ = j.get("commitment", "")
            cur = conn.execute("SELECT url FROM jobs WHERE url = ?", (j["url"],))
            if cur.fetchone() is None:
                new_count += 1
                conn.execute(
                    "INSERT INTO jobs (url, title, company, location, posted, source, department, "
                    "commitment, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (j["url"], j["title"], j["company"], j["location"], j["posted"], j["source"],
                     dept, commit_, now, now),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET title=?, company=?, location=?, posted=?, source=?, department=?, "
                    "commitment=?, last_seen=? WHERE url=?",
                    (j["title"], j["company"], j["location"], j["posted"], j["source"], dept, commit_,
                     now, j["url"]),
                )
        conn.commit()
    return new_count


def prune_stale(max_age_days=10):
    """Drop jobs not seen in the last N scrape cycles (i.e. likely closed/filled)."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with _lock, get_conn() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE last_seen < ?", (cutoff,))
        conn.commit()
        return cur.rowcount


def record_run(started_at, finished_at, status, stats, jobs_in_db_after):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO scrape_runs (started_at, finished_at, status, companies_scanned, "
            "companies_with_jobs, jobs_scraped, jobs_in_db_after) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (started_at, finished_at, status, stats.get("companies_scanned", 0),
             stats.get("companies_with_jobs", 0), stats.get("jobs_scraped", 0), jobs_in_db_after),
        )
        conn.commit()


def last_run():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def total_jobs():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]


def search_jobs(query="", location="", days=None, department="", commitment="", page=1, per_page=25):
    """Boolean keyword search against title (AND/OR/NOT/quotes/parens via
    boolean_search.py), plus facet filters for location substring, days-back,
    department, and commitment. Boolean evaluation happens in Python; SQL is
    only used to (a) apply the facet filters and (b) cheaply narrow the
    candidate set using a safe superset (OR of all non-negated terms) before
    evaluating the full expression.
    """
    ast = parse_query(query)

    where = []
    params = []

    if location.strip():
        where.append("(location LIKE ? OR location = '' OR location IS NULL)")
        params.append(f"%{location.strip()}%")

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

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs {where_sql} ORDER BY (posted IS NULL), posted DESC, company ASC "
            f"LIMIT ?",
            params + [MAX_CANDIDATES],
        ).fetchall()

    candidates = [dict(r) for r in rows]
    if ast is not None:
        candidates = [r for r in candidates if evaluate(ast, r["title"])]

    total = len(candidates)
    offset = max(0, (page - 1)) * per_page
    page_rows = candidates[offset: offset + per_page]
    return page_rows, total


def distinct_facet_values(column, limit=30):
    """Top N non-empty distinct values for a facet column, ordered by frequency."""
    assert column in ("department", "commitment")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {column} AS v, COUNT(*) AS c FROM jobs WHERE {column} != '' "
            f"GROUP BY {column} ORDER BY c DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row["v"] for row in rows]
