"""
db_users.py — Postgres-backed layer for user accounts, saved searches, and
applied-job tracking.

Deliberately a SEPARATE database from db.py's SQLite job cache, connected
via the DATABASE_URL environment variable (the standard Render/Heroku
convention: postgres://user:pass@host:port/dbname). The job cache is
disposable — if it's ever lost, the next scrape rebuilds it from scratch.
A user's account and saved data are NOT disposable, so they don't share
storage with data that's designed to be wiped/rebuilt. See README's "User
accounts" section for the full reasoning, including why SQLite (and
Render's free/expiring Postgres tier) were both ruled out for this.

If DATABASE_URL isn't set at all (local dev without Postgres configured,
or a deployment that hasn't opted into accounts), every function here
raises a clear RuntimeError rather than crashing the whole app at import
time — app.py checks `accounts_enabled()` and returns a friendly 503 on
the account-related routes instead of letting these errors surface raw.
Search/browse/resume-match, none of which touch this file, keep working
exactly as before regardless.
"""

import os
from contextlib import contextmanager

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.errors
except ImportError:  # psycopg2 itself might not be installed in a
    # deployment that never intends to use accounts -- keep that a soft
    # failure too, same as a missing DATABASE_URL.
    psycopg2 = None

DATABASE_URL = os.environ.get("DATABASE_URL")


def accounts_enabled():
    """True if this deployment is configured for user accounts at all
    (both the driver and a connection string are present). app.py's
    account routes check this first and return a clear 503 instead of a
    raw exception when it's False."""
    return psycopg2 is not None and bool(DATABASE_URL)


def get_conn():
    if not accounts_enabled():
        raise RuntimeError(
            "User accounts aren't configured on this deployment -- "
            "DATABASE_URL is not set (or psycopg2 isn't installed). See README's "
            "'User accounts' section for how to provision a Postgres database."
        )
    return psycopg2.connect(DATABASE_URL)


@contextmanager
def conn_ctx():
    """Commit-on-success / rollback-on-error / always-close, same pattern
    as db.py's conn_ctx() for the SQLite side."""
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
    """Creates the three account-related tables if they don't exist yet.
    Safe to call on every startup, same as db.init_db()."""
    with conn_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS saved_searches (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_saved_searches_user ON saved_searches(user_id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS applied_jobs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    job_url TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (user_id, job_url)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_applied_jobs_user ON applied_jobs(user_id)")


# ---------------- users ----------------

def create_user(email, password_hash):
    """Raises psycopg2.errors.UniqueViolation if the email's already
    registered -- app.py catches that specifically to return a friendly
    "that email's already in use" message instead of a generic 500."""
    email = email.strip().lower()
    with conn_ctx() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) "
                "RETURNING id, email, created_at",
                (email, password_hash),
            )
            return dict(cur.fetchone())


def get_user_by_email(email):
    email = email.strip().lower()
    with conn_ctx() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_user_by_id(user_id):
    with conn_ctx() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, email, created_at FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None


# ---------------- saved searches ----------------

def list_saved_searches(user_id):
    with conn_ctx() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, name, params_json, created_at FROM saved_searches "
                "WHERE user_id = %s ORDER BY created_at DESC",
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def create_saved_search(user_id, name, params_json):
    with conn_ctx() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO saved_searches (user_id, name, params_json) VALUES (%s, %s, %s) "
                "RETURNING id, name, params_json, created_at",
                (user_id, name, params_json),
            )
            return dict(cur.fetchone())


def delete_saved_search(user_id, search_id):
    """Returns True if a row was actually deleted -- scoped to user_id too
    so one user can never delete another's saved search just by guessing
    an id."""
    with conn_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM saved_searches WHERE id = %s AND user_id = %s",
                (search_id, user_id),
            )
            return cur.rowcount > 0


# ---------------- applied jobs ----------------

def list_applied_job_urls(user_id):
    """Just the URLs, as a set -- used to badge job cards in a search
    result (one query per request, then an in-memory set-membership check
    per row, instead of a query per card)."""
    with conn_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT job_url FROM applied_jobs WHERE user_id = %s", (user_id,))
            return {row[0] for row in cur.fetchall()}


def list_applied_jobs(user_id):
    with conn_ctx() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT job_url, applied_at FROM applied_jobs "
                "WHERE user_id = %s ORDER BY applied_at DESC",
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def mark_applied(user_id, job_url):
    """Idempotent -- marking an already-applied job again is a no-op, not
    an error, via ON CONFLICT DO NOTHING against the (user_id, job_url)
    unique constraint."""
    with conn_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applied_jobs (user_id, job_url) VALUES (%s, %s) "
                "ON CONFLICT (user_id, job_url) DO NOTHING",
                (user_id, job_url),
            )


def unmark_applied(user_id, job_url):
    with conn_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM applied_jobs WHERE user_id = %s AND job_url = %s",
                (user_id, job_url),
            )
