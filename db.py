"""
db.py — tiny SQLite layer for the job cache.
"""

import sqlite3
import os
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("JOBS_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.db"))

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
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title)")
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
            cur = conn.execute("SELECT url FROM jobs WHERE url = ?", (j["url"],))
            if cur.fetchone() is None:
                new_count += 1
                conn.execute(
                    "INSERT INTO jobs (url, title, company, location, posted, source, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (j["url"], j["title"], j["company"], j["location"], j["posted"], j["source"], now, now),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET title=?, company=?, location=?, posted=?, source=?, last_seen=? WHERE url=?",
                    (j["title"], j["company"], j["location"], j["posted"], j["source"], now, j["url"]),
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


def search_jobs(query="", location="", days=None, page=1, per_page=25):
    """Keyword search (OR across whitespace/comma-separated terms, matched against
    title), optional location substring match, optional days-back filter on `posted`."""
    where = []
    params = []

    terms = [t.strip() for t in re_split(query) if t.strip()]
    if terms:
        clauses = []
        for t in terms:
            clauses.append("title LIKE ?")
            params.append(f"%{t}%")
        where.append("(" + " OR ".join(clauses) + ")")

    if location.strip():
        where.append("(location LIKE ? OR location = '' OR location IS NULL)")
        params.append(f"%{location.strip()}%")

    if days:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%d")
        where.append("(posted >= ? OR posted IS NULL OR posted = '')")
        params.append(cutoff)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    offset = max(0, (page - 1)) * per_page

    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM jobs {where_sql}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM jobs {where_sql} ORDER BY (posted IS NULL), posted DESC, company ASC "
            f"LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
        return [dict(r) for r in rows], total


def re_split(s):
    import re
    return re.split(r"[,\n]+", s or "")
