"""
app.py — Flask web app: serves the search UI + JSON API, and runs the
background scraper on a schedule.

Run locally:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:8000
"""

import functools
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.error import URLError

from flask import Flask, Response, jsonify, request, send_from_directory, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import db
import db_users
from scraper import scrape_all
from companies_data import COMPANIES
import resume_parser
from location_groups import (
    US_WORD_RE, US_STATE_NAMES, US_STATE_ABBR_RE,
    CANADA_WORD_RE, CANADA_PROVINCE_NAMES, UK_WORD_RE,
)
from role_groups import ROLE_LABELS_BY_SLUG
import mcp_server

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")

SCRAPE_INTERVAL_HOURS = float(os.environ.get("SCRAPE_INTERVAL_HOURS", "8"))
MAX_WORKERS = int(os.environ.get("SCRAPE_MAX_WORKERS", "4"))
# How many days a job can go unseen by the scraper before db.prune_stale()
# drops it (i.e. it's presumed closed/filled). Shared with the /jobs/<id>
# detail page's JobPosting structured data below (see JOB_SITEMAP_PAGE_SIZE
# and render_job_page()) so a listing's advertised validThrough date
# actually lines up with when this app itself will make the page 404 --
# was a hardcoded `10` inline at the one call site before this; pulled out
# so the two places that need this number can't quietly drift apart.
JOB_STALE_DAYS = 10
MAX_RESUME_BYTES = 8 * 1024 * 1024  # 8 MB
ALLOWED_RESUME_EXT = (".pdf", ".docx", ".txt")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Skip The Boards <onboarding@resend.dev>")
# Used to build the link inside the reset email (e.g. "https://open-roles-
# finder.onrender.com") -- deliberately an explicit env var rather than
# inferred from request.url_root, since that can be wrong behind a proxy/
# load balancer. Falls back to request.url_root at send-time if unset,
# which is fine for local dev but should be set explicitly in production.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
# Absolute site origin for SEO purposes only (canonical link tags, Open
# Graph/Twitter og:url, robots.txt's Sitemap: line, and sitemap.xml's own
# <loc> entries) -- deliberately NOT built from request.url_root the way
# the password-reset link above is. This app has no ProxyFix configured,
# and Render/Cloudflare terminate TLS in front of it, so request.url_root
# can't be trusted to report "https://" reliably without one; getting an
# "http://" canonical URL into a page that's only ever served over https
# is exactly the kind of thing that quietly confuses search engines.
# Reuses APP_BASE_URL if that's already set (same env var, same meaning),
# otherwise falls back to this app's actual production domain rather than
# guessing from the request.
SITE_URL = APP_BASE_URL or "https://skiptheboards.com"
RESET_TOKEN_TTL_HOURS = 1
# Optional GA4 traffic tracking -- off unless set, same graceful-
# degradation pattern as everything else here. Injected client-side (see
# GET /api/site-config + app.js's loadSiteConfig()) rather than baked into
# static/index.html directly, so the measurement ID isn't hardcoded into
# version control and can be changed via env var alone.
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "")
# Optional contact form -- off unless RESEND_API_KEY (already used for
# password reset) AND a destination address are both set. CONTACT_EMAIL
# defaults to the site owner's own address rather than empty, since this
# is a single-operator app; override via env var if that ever changes.
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "jarededberg@gmail.com")
MAX_CONTACT_MESSAGE_LEN = 4000
# /admin (signup-count dashboard) -- gated on the logged-in session's email
# matching this, not a separate password, so there's no extra credential to
# manage. Defaults to the site owner's own address for the same single-
# operator reasoning as CONTACT_EMAIL above; override via env var if that
# ever changes. Compared case-insensitively in admin_required() since
# db_users.create_user() already lowercases emails on signup, but this
# constant is operator-typed and shouldn't have to match that casing.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "jarededberg@gmail.com").strip().lower()

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = MAX_RESUME_BYTES
# Session cookies need a stable secret to sign against. Falling back to a
# freshly-generated one when SECRET_KEY isn't set keeps the app from
# crashing on startup, but it means every process restart invalidates
# every logged-in session (everyone gets silently logged out) -- fine for
# local dev, NOT fine on a host that restarts/redeploys periodically. Set
# a real SECRET_KEY env var in production; see README's "User accounts"
# section.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("SECRET_KEY"):
    print("[app] WARNING: SECRET_KEY not set -- using a random key that "
          "will change on every restart, logging out all users each time. "
          "Set SECRET_KEY in production.")

# Rate limiting -- primarily to blunt bot signup/login floods (see README's
# "User accounts" section for the cost reasoning). In-memory storage is
# fine for a single web instance (Render's free/Starter/Basic tiers all run
# exactly one); if this app ever scales to multiple instances, the limits
# stop being shared across them and a Redis storage_uri should be added.
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri="memory://",
    default_limits=[],  # only the routes below get limited, not everything
)

_scrape_lock = threading.Lock()
_scrape_in_progress = False
_scrape_progress = {"done": 0, "total": 0}


def run_scrape_job():
    global _scrape_in_progress, _scrape_progress
    if not _scrape_lock.acquire(blocking=False):
        return  # a scrape is already running
    _scrape_in_progress = True
    _scrape_progress = {"done": 0, "total": len(COMPANIES)}
    started = datetime.now(timezone.utc).isoformat()
    try:
        def progress_cb(done, total):
            _scrape_progress["done"] = done
            _scrape_progress["total"] = total

        new_count = 0

        def batch_cb(jobs_batch):
            nonlocal new_count
            new_count += db.upsert_jobs(jobs_batch)

        platform_cache = db.get_platform_cache()
        platform_updates = {}

        def platform_cb(slug, platform):
            platform_updates[slug] = platform

        _, stats = scrape_all(max_workers=MAX_WORKERS, progress_cb=progress_cb, batch_cb=batch_cb,
                               platform_cache=platform_cache, platform_cb=platform_cb)
        db.upsert_platform_cache(platform_updates)
        removed = db.prune_stale(max_age_days=JOB_STALE_DAYS)
        finished = datetime.now(timezone.utc).isoformat()
        db.record_run(started, finished, "ok", stats, db.total_jobs())
        _check_scrape_health(stats)
        print(f"[scrape] done: {stats}, new={new_count}, pruned={removed}, "
              f"platform_cache={len(platform_cache)}->{len(platform_updates)} updates")
    except Exception as e:
        finished = datetime.now(timezone.utc).isoformat()
        db.record_run(started, finished, f"error: {e}", {}, db.total_jobs())
        print(f"[scrape] FAILED: {e}")
    finally:
        _scrape_in_progress = False
        _scrape_lock.release()


_HEALTH_ALERT_MIN_RUNS = 5  # need at least this many prior runs before trusting a "spike" comparison
_HEALTH_ALERT_RATIO = 1.5   # not_found rate has to be 50% worse than the recent average to alert
_HEALTH_ALERT_MIN_NOT_FOUND = 50  # ...and it has to be a meaningfully large number, not noise on a tiny scan


def _check_scrape_health(stats):
    """Compares this run's companies_not_found against the average of the
    last _HEALTH_ALERT_MIN_RUNS runs (excluding this one). A sudden jump
    usually means something broke on Claude's/Jared's end (a bad
    companies_data.py edit, a code regression in scraper.py) rather than
    hundreds of companies coincidentally shutting down their career pages
    on the same day, so it's worth a one-off email rather than silently
    scrolling past it in server logs. Deliberately conservative (needs
    history AND a real ratio AND a real absolute count) so this doesn't
    cry wolf on small fluctuations or during the first few runs after a
    fresh deploy when there's no baseline yet."""
    if not (RESEND_API_KEY and CONTACT_EMAIL):
        return
    not_found = stats.get("companies_not_found", 0)
    scanned = stats.get("companies_scanned", 0) or 1
    history = db.recent_runs(limit=_HEALTH_ALERT_MIN_RUNS + 1)[1:]  # drop the run just recorded
    if len(history) < _HEALTH_ALERT_MIN_RUNS:
        return
    baseline = [h.get("companies_not_found") or 0 for h in history]
    avg_baseline = sum(baseline) / len(baseline)
    if not_found < _HEALTH_ALERT_MIN_NOT_FOUND:
        return
    if avg_baseline > 0 and not_found < avg_baseline * _HEALTH_ALERT_RATIO:
        return
    if avg_baseline == 0 and not_found < _HEALTH_ALERT_MIN_NOT_FOUND:
        return
    subject = f"Skip The Boards: scrape health alert ({not_found} companies not found)"
    body = (
        f"<p>The latest scrape run found {not_found} of {scanned} companies "
        f"not resolving on any ATS platform (Greenhouse/Lever/Ashby).</p>"
        f"<p>Recent baseline average: {avg_baseline:.1f} not-found per run "
        f"(over the last {len(baseline)} runs).</p>"
        f"<p>This could mean a bad edit to companies_data.py, a code "
        f"regression in scraper.py, or a real outage on one of the three "
        f"ATS platforms' APIs -- worth a quick look.</p>"
    )
    payload = json.dumps({
        "from": RESEND_FROM_EMAIL,
        "to": [CONTACT_EMAIL],
        "subject": subject,
        "html": body,
    }).encode("utf-8")
    _post_to_resend(payload)


def _compute_next_scrape_time():
    """When to schedule the *first* run of the recurring scrape job at
    startup. Deliberately NOT "always right now" -- that was the previous
    behavior (a bare `next_run_time=datetime.now()`), which meant every
    single code deploy, even a one-line change, kicked off a full re-
    scrape of all ~4,300 companies immediately. That scraper shares this
    process's single gunicorn worker (and its GIL) with every web
    request, so for the ~30 minutes a full scrape takes, it can starve
    other threads of CPU time badly enough to make an otherwise-healthy
    request (confirmed independently: DNS, TLS, and the Resend API itself
    all responded instantly when tested directly, outside this process)
    look hung from the app's own perspective -- exactly what happened
    testing the contact form shortly after a deploy.

    JOBS_DB_PATH lives on a persistent disk (see README), so scrape
    history normally survives across deploys. This uses that history to
    schedule sensibly instead of restarting the clock on every restart:
      - Never scraped before (fresh disk / brand-new deployment): run
        right away, same as before -- there's no data to serve otherwise.
      - Last scrape started less than SCRAPE_INTERVAL_HOURS ago: schedule
        the first run for whenever it was actually due, not immediately.
      - Overdue (the app was down past its next scheduled time, or this
        is an old deploy with SCRAPE_INTERVAL_HOURS lowered since): run
        right away -- being overdue for the periodic scrape is exactly
        the case an unattended scheduler is supposed to catch up on."""
    last = db.last_run()
    if last is None:
        return datetime.now()
    started_at = last.get("started_at")
    try:
        last_start = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return datetime.now()
    if last_start.tzinfo is None:
        last_start = last_start.replace(tzinfo=timezone.utc)
    due = last_start + timedelta(hours=SCRAPE_INTERVAL_HOURS)
    now = datetime.now(timezone.utc)
    return due if due > now else now


def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(run_scrape_job, "interval", hours=SCRAPE_INTERVAL_HOURS, id="scrape",
                       next_run_time=_compute_next_scrape_time())
    # Runs once a day, independent of the (much more frequent) scrape
    # cadence -- alerting on every single scrape cycle (every
    # SCRAPE_INTERVAL_HOURS, default 8h/3x a day) would mean up to 3
    # emails/day per saved search, which is spam, not a digest.
    scheduler.add_job(run_saved_search_alerts_job, "interval", hours=24, id="search_alerts",
                       next_run_time=_compute_next_alert_time())
    scheduler.add_job(run_mcp_search_alerts_job, "interval", hours=24, id="mcp_search_alerts",
                       next_run_time=_compute_next_mcp_alert_time())
    scheduler.add_job(run_weekly_digest_job, "interval", days=7, id="weekly_digest",
                       next_run_time=_compute_next_digest_time())
    scheduler.start()
    return scheduler


# ---------------- accounts: helpers ----------------

def turnstile_enabled():
    return bool(TURNSTILE_SECRET_KEY)


def verify_turnstile(token, remote_ip):
    """Checks a Cloudflare Turnstile token against Cloudflare's siteverify
    endpoint. Returns True/False. If TURNSTILE_SECRET_KEY isn't set, this
    deployment hasn't configured Turnstile at all -- skip the check
    entirely (return True) rather than locking everyone out, same
    graceful-degradation pattern as accounts_required/DATABASE_URL. See
    README's "User accounts" section for setup steps."""
    if not turnstile_enabled():
        return True
    if not token:
        return False
    payload = json.dumps({
        "secret": TURNSTILE_SECRET_KEY,
        "response": token,
        "remoteip": remote_ip or "",
    }).encode("utf-8")
    req = urllib.request.Request(
        TURNSTILE_VERIFY_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return bool(result.get("success"))
    except (URLError, TimeoutError, ValueError):
        # Cloudflare's endpoint being unreachable shouldn't be the reason a
        # real person can't sign up -- fail open here, since the rate
        # limiter above is the primary defense and Turnstile is a second
        # layer, not the only one.
        return True


def password_reset_enabled():
    return bool(RESEND_API_KEY)


def _post_to_resend(payload, hard_timeout=10, socket_timeout=8):
    """POSTs an already-JSON-encoded payload (bytes) to Resend's REST API
    and returns True/False, never raising.

    This exists because `urllib.request.urlopen(req, timeout=socket_timeout)`
    alone is NOT a reliable upper bound on how long this can take. The
    timeout param only covers the connect/read phases of the socket -- DNS
    resolution (getaddrinfo, which urlopen calls internally before it ever
    gets a socket to apply that timeout to) can still hang indefinitely on
    some networks, ignoring the timeout entirely. That's exactly what
    happened in production here: a valid contact-form submission hung for
    120+ seconds with zero response (confirmed via curl -- an invalid
    payload that fails validation before ever reaching this function still
    returns in well under a second, so the route itself is fine; it's
    specifically the network call out to api.resend.com that stalls),
    which lines up with gunicorn's own --timeout 120 eventually killing the
    worker rather than urlopen's timeout=8 ever firing.

    The fix is a real wall-clock cutoff that doesn't depend on the socket
    layer cooperating: run the request in a daemon thread and join() it
    with a hard timeout. If Resend (or DNS to it) is unreachable and the
    inner call hangs forever, this function still returns within
    `hard_timeout` seconds -- the leaked thread dies on its own once the
    call eventually resolves/errors, and being a daemon thread means it
    can't block process shutdown either way."""
    result = {"ok": False}

    def worker():
        try:
            req = urllib.request.Request(
                "https://api.resend.com/emails", data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
                result["ok"] = 200 <= resp.status < 300
        except Exception as e:
            # Broad except deliberately -- URLError/HTTPError/timeout/
            # whatever else, none of it should ever bubble up into the
            # request handler as a 500. Logged for the operator only (e.g.
            # RESEND_API_KEY typo'd, or Resend's sandbox restriction
            # blocking delivery because a custom domain hasn't been
            # verified yet -- see README); the caller shows the same
            # generic response to the browser regardless.
            result["ok"] = False
            result["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=hard_timeout)
    if t.is_alive():
        print(f"[app] Resend call still hanging after {hard_timeout}s "
              f"(DNS/connect to api.resend.com not responding) -- giving "
              f"up and returning failure to the caller instead of blocking "
              f"the request indefinitely.")
        return False
    if "error" in result:
        print(f"[app] Resend call failed: {result['error']}")
    return result["ok"]


def send_password_reset_email(to_email, reset_link):
    """Sends the reset-link email via Resend's REST API -- plain urllib
    (via _post_to_resend, see its docstring for why that's wrapped in a
    hard-timeout thread), so this doesn't need to pull in an HTTP client
    dependency (or the `resend` package) for one call. Returns True/False;
    the caller always shows the same generic response to the browser
    regardless of the result (see api_forgot_password), so a delivery
    failure here only ever surfaces in the server logs, never as a signal
    to whoever's making the request about whether the email exists or
    whether sending succeeded."""
    payload = json.dumps({
        "from": RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": "Reset your Skip The Boards password",
        "html": (
            "<p>Someone (hopefully you) asked to reset the password on your "
            "Skip The Boards account.</p>"
            f'<p><a href="{reset_link}">Click here to set a new password</a>. '
            f"This link expires in {RESET_TOKEN_TTL_HOURS} hour"
            f"{'s' if RESET_TOKEN_TTL_HOURS != 1 else ''}.</p>"
            "<p>If you didn't request this, you can safely ignore this "
            "email -- your password hasn't been changed.</p>"
        ),
    }).encode("utf-8")
    return _post_to_resend(payload)


def login_required(f):
    """Route decorator -- returns 401 instead of running the view at all
    if there's no logged-in user. Applied to every saved-search/applied-
    job route below; /api/jobs itself stays open to everyone (accounts
    are optional, not a gate on searching)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            # "message" (not "error") to match every other account-route
            # response shape -- app.js's error handling uniformly reads
            # data.message, so a mismatched key here silently fell back to
            # a generic "Something went wrong." instead of this actual text.
            return jsonify({"ok": False, "message": "Log in to use this feature."}), 401
        return f(*args, **kwargs)
    return wrapper


def accounts_required(f):
    """Returns a clear 503 if this deployment has no DATABASE_URL
    configured at all, rather than letting a raw psycopg2 connection
    error bubble up as a 500. Stacked with login_required on routes that
    need both checks (accounts configured AND a logged-in user)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not db_users.accounts_enabled():
            return jsonify({"ok": False, "message": "Accounts aren't set up on this deployment yet."}), 503
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Gates /api/admin/* routes to the single account whose email matches
    ADMIN_EMAIL. Stacked *after* accounts_required + login_required (so it
    can assume session["user_id"] exists and accounts are configured) on
    every admin route. Returns 403 rather than a redirect or a rendered
    "not authorized" page -- this only guards a JSON API; admin.html itself
    is a static file anyone can request, but it renders nothing until this
    endpoint returns real data, so serving the static shell isn't a leak."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        user = db_users.get_user_by_id(session["user_id"])
        if not user or user["email"].strip().lower() != ADMIN_EMAIL:
            return jsonify({"ok": False, "message": "Not authorized."}), 403
        return f(*args, **kwargs)
    return wrapper


def contact_enabled():
    return bool(RESEND_API_KEY and CONTACT_EMAIL)


CONTACT_REASONS = (
    "General question",
    "Add my company's job board",
    "Add a specific role",
    "Bug report",
    "Other",
)


def send_contact_email(reason, name, from_email, message):
    """Sends a contact-form submission to CONTACT_EMAIL via Resend, same
    urllib pattern as send_password_reset_email() (both go through
    _post_to_resend(), see its docstring for why the network call is
    wrapped in a hard-timeout thread rather than trusting urlopen's own
    timeout param). Sets reply_to to the submitter's own address so
    replying from the inbox goes straight back to them, rather than to
    Resend's From address. Returns True/False; the caller always shows the
    same success message to the browser regardless (see api_contact) so a
    delivery failure only ever shows up in server logs. `reason` is put in
    the subject line so submissions (general questions vs. "add my
    company" vs. bug reports, etc.) are triageable from the inbox without
    opening every one."""
    # Escaped before going into the HTML body -- reason/name/message are
    # all user-supplied, and this is the one email-sending path on the
    # site where the sender picks the content freely (unlike the
    # password-reset email, which never embeds anything user-typed).
    safe_reason = html.escape(reason)
    safe_name = html.escape(name)
    safe_email = html.escape(from_email)
    safe_message = html.escape(message).replace("\n", "<br>")
    payload = json.dumps({
        "from": RESEND_FROM_EMAIL,
        "to": [CONTACT_EMAIL],
        "reply_to": from_email,
        "subject": f"Skip The Boards contact form: {reason} ({name})",
        "html": (
            f"<p><strong>Reason:</strong> {safe_reason}</p>"
            f"<p><strong>From:</strong> {safe_name} ({safe_email})</p>"
            f"<p>{safe_message}</p>"
        ),
    }).encode("utf-8")
    return _post_to_resend(payload)


# ---------------- saved-search email alerts ----------------

SEARCH_ALERT_MAX_JOBS_PER_EMAIL = 25  # caps the email body; subject line still states the true total
SEARCH_ALERT_FETCH_LIMIT = 500  # how many matching rows to pull before filtering to "new since last check"


def _saved_search_to_query_kwargs(params):
    """Maps the free-form dict a saved search stores (see app.js's
    currentSearchParams()) onto db.search_jobs()'s keyword arguments.
    Tolerant of older saved-search shapes (a singular `department`/
    `location` from before the multi-select arrays existed) by falling
    back to them only when the array form isn't present, same precedence
    db.search_jobs() itself documents."""
    kwargs = {
        "query": params.get("q", "") or "",
        "days": params.get("days") or None,
        "commitment": params.get("commitment", "") or "",
    }
    departments = params.get("departments")
    if departments:
        kwargs["departments"] = departments
    elif params.get("department"):
        kwargs["department"] = params.get("department")
    locations = params.get("locations")
    if locations:
        kwargs["locations"] = locations
    elif params.get("location"):
        kwargs["location"] = params.get("location")
    for key in ("salary_min", "salary_max", "yoe_min", "yoe_max"):
        if params.get(key) is not None:
            kwargs[key] = params[key]
    return kwargs


def send_saved_search_alert_email(to_email, search_name, jobs):
    """Sends a digest of newly-matching jobs for one saved search, via the
    same Resend path as the contact form / password reset (see
    _post_to_resend's docstring for the hard-timeout reasoning). Uses
    html.escape() directly rather than render_job_page()'s local `esc`
    helper, which is scoped inside that function and not reachable here."""
    esc = html.escape
    shown = jobs[:SEARCH_ALERT_MAX_JOBS_PER_EMAIL]
    rows_html = "".join(
        f"<li><a href=\"{esc(SITE_URL + _job_path(j['job_id'], j['company'], j['title']))}\">"
        f"{esc(j['title'])}</a> — {esc(j['company'])}"
        f"{' — ' + esc(j['location']) if j.get('location') else ''}</li>"
        for j in shown
    )
    more_note = (
        f"<p>...and {len(jobs) - len(shown)} more. "
        f"<a href=\"{esc(SITE_URL)}/\">See all on Skip The Boards</a>.</p>"
        if len(jobs) > len(shown) else ""
    )
    subject = f"Skip The Boards: {len(jobs)} new job{'s' if len(jobs) != 1 else ''} for \"{search_name}\""
    payload = json.dumps({
        "from": RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": (
            f"<p>New postings matching your saved search \"{esc(search_name)}\":</p>"
            f"<ul>{rows_html}</ul>"
            f"{more_note}"
            f"<p style=\"color:#888;font-size:12px;\">You're getting this because email "
            f"alerts are on for this saved search. Turn them off anytime from "
            f"\"Saved searches\" on the site.</p>"
        ),
    }).encode("utf-8")
    return _post_to_resend(payload)


def run_saved_search_alerts_job():
    """Runs once a day (see start_scheduler()): checks every saved search
    with alerts_enabled, finds jobs newly discovered (by first_seen --
    when THIS site first scraped it, not the ATS's own `posted` date,
    which is sometimes backdated or missing) since that search was last
    checked, and emails a digest if there are any. Applies the search's
    own saved filters as-is on top of that (so a search saved with
    `days=3` still only alerts on jobs matching that filter too)."""
    if not db_users.accounts_enabled():
        return
    if not (RESEND_API_KEY and RESEND_FROM_EMAIL):
        return
    searches = db_users.list_searches_for_alerts()
    emails_sent = 0
    now = datetime.now(timezone.utc)
    for s in searches:
        try:
            params = json.loads(s["params_json"])
        except (TypeError, ValueError):
            params = {}
        cutoff = s.get("last_checked_at") or s.get("created_at")
        if cutoff and cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        kwargs = _saved_search_to_query_kwargs(params)
        try:
            rows, _total = db.search_jobs(**kwargs, page=1, per_page=SEARCH_ALERT_FETCH_LIMIT)
        except Exception as e:
            print(f"[alerts] search failed for saved search {s['id']}: {e}")
            continue
        new_rows = []
        for r in rows:
            first_seen = r.get("first_seen")
            if not first_seen:
                continue
            try:
                fs = datetime.fromisoformat(first_seen)
            except ValueError:
                continue
            if fs.tzinfo is None:
                fs = fs.replace(tzinfo=timezone.utc)
            if not cutoff or fs > cutoff:
                new_rows.append(r)
        if new_rows:
            if send_saved_search_alert_email(s["email"], s["name"], new_rows):
                emails_sent += 1
        db_users.mark_search_checked(s["id"], now)
    db_users.record_alert_run(now, len(searches), emails_sent)
    print(f"[alerts] checked {len(searches)} saved searches, sent {emails_sent} emails")


def run_mcp_search_alerts_job():
    """The MCP-tool equivalent of run_saved_search_alerts_job() above --
    same "new since last check, by first_seen" logic and the same
    send_saved_search_alert_email() (that function only cares about
    to_email/search_name/jobs, nothing about where the search itself was
    stored, so it's reused as-is rather than duplicated) -- but reading
    from db.py's mcp_saved_searches table (discrete columns, no JSON
    blob, no login) instead of db_users.py's saved_searches. Kept as a
    separate function/scheduled job rather than merged into the other
    one specifically so a bug or slowdown in one alert path can never
    affect the other, and so MCP-created alerts keep working even on a
    deployment with no Postgres/DATABASE_URL configured at all (accounts
    disabled) -- this path never touches db_users."""
    if not (RESEND_API_KEY and RESEND_FROM_EMAIL):
        return
    searches = db.list_mcp_searches_for_alerts()
    emails_sent = 0
    now = datetime.now(timezone.utc)
    for s in searches:
        cutoff = s.get("last_checked_at") or s.get("created_at")
        try:
            cutoff = datetime.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
        except (TypeError, ValueError):
            cutoff = None
        if cutoff and cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        kwargs = {
            "query": s.get("query") or "",
            "location": s.get("location") or "",
            "department": s.get("department") or "",
            "commitment": s.get("commitment") or "",
        }
        if s.get("location_group"):
            kwargs["location_groups"] = [s["location_group"]]
        try:
            rows, _total = db.search_jobs(**kwargs, page=1, per_page=SEARCH_ALERT_FETCH_LIMIT)
        except Exception as e:
            print(f"[mcp-alerts] search failed for mcp saved search {s['id']}: {e}")
            continue
        new_rows = []
        for r in rows:
            first_seen = r.get("first_seen")
            if not first_seen:
                continue
            try:
                fs = datetime.fromisoformat(first_seen)
            except ValueError:
                continue
            if fs.tzinfo is None:
                fs = fs.replace(tzinfo=timezone.utc)
            if not cutoff or fs > cutoff:
                new_rows.append(r)
        if new_rows:
            name = s.get("name") or "your MCP search"
            if send_saved_search_alert_email(s["email"], name, new_rows):
                emails_sent += 1
        db.mark_mcp_search_checked(s["id"], now)
    print(f"[mcp-alerts] checked {len(searches)} MCP-created alerts, sent {emails_sent} emails")


def _compute_next_mcp_alert_time():
    """Same reasoning as _compute_next_alert_time() -- schedule off the
    last recorded activity instead of firing on every deploy. No
    dedicated "run" record for this job (unlike the other three
    scheduled jobs) since mcp_saved_searches rows themselves carry
    last_checked_at -- this just looks at the single most-recently-
    checked row as a stand-in for "when did this job last actually run."
    """
    try:
        rows = db.list_mcp_searches_for_alerts()
    except Exception:
        return datetime.now() + timedelta(hours=1)
    checked_ats = [r.get("last_checked_at") for r in rows if r.get("last_checked_at")]
    if not checked_ats:
        return datetime.now()
    last = max(checked_ats)
    try:
        last = datetime.fromisoformat(last) if isinstance(last, str) else last
    except (TypeError, ValueError):
        return datetime.now()
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    due = last + timedelta(hours=24)
    now = datetime.now(timezone.utc)
    return due if due > now else now


def run_weekly_digest_job():
    """Sends the same list /weekly-digest shows publicly to every active
    subscriber, once a week. Requires _fetch_digest_jobs() (defined
    further down this file, after REMOTE_HUB_GROUPS/render_hub_page) to
    already be resolvable at call time -- fine, since this only ever runs
    from the scheduler, long after the whole module has finished
    importing, not at import time itself."""
    if not (RESEND_API_KEY and RESEND_FROM_EMAIL):
        return
    subscribers = db.list_active_digest_subscribers()
    if not subscribers:
        db.record_digest_run(datetime.now(timezone.utc), 0)
        return
    jobs = _fetch_digest_jobs()
    rows_html = "".join(
        f"<li><a href=\"{html.escape(SITE_URL + _job_path(j['job_id'], j['company'], j['title']))}\">"
        f"{html.escape(j['title'])}</a> — {html.escape(j['company'])}"
        f"{' — ' + html.escape(_job_salary_text(j)) if _job_salary_text(j) else ''}</li>"
        for j in jobs
    )
    sent = 0
    for email in subscribers:
        token = _digest_unsub_token(email)
        unsub_url = f"{SITE_URL}/unsubscribe?email={urllib.parse.quote(email)}&token={token}"
        payload = json.dumps({
            "from": RESEND_FROM_EMAIL,
            "to": [email],
            "subject": f"Skip The Boards: {len(jobs)} best remote roles this week",
            "html": (
                f"<p>This week's highest-paying remote postings from the last 7 days:</p>"
                f"<ul>{rows_html}</ul>"
                f"<p><a href=\"{html.escape(SITE_URL)}/weekly-digest\">See the full, always-current list</a></p>"
                f"<p style=\"color:#888;font-size:12px;\">"
                f"<a href=\"{html.escape(unsub_url)}\">Unsubscribe</a> from this weekly email.</p>"
            ),
        }).encode("utf-8")
        if _post_to_resend(payload):
            sent += 1
    db.record_digest_run(datetime.now(timezone.utc), sent)
    print(f"[digest] sent to {sent}/{len(subscribers)} subscribers")


def _compute_next_digest_time():
    """Same reasoning as _compute_next_scrape_time()/_compute_next_alert_time()
    -- schedule off the last recorded run instead of firing on every deploy."""
    try:
        last = db.last_digest_run()
    except Exception:
        return datetime.now() + timedelta(hours=1)
    if last is None:
        return datetime.now()
    ran_at = last.get("ran_at")
    try:
        ran_at = datetime.fromisoformat(ran_at) if isinstance(ran_at, str) else ran_at
    except (TypeError, ValueError):
        return datetime.now()
    if ran_at is None:
        return datetime.now()
    if ran_at.tzinfo is None:
        ran_at = ran_at.replace(tzinfo=timezone.utc)
    due = ran_at + timedelta(days=7)
    now = datetime.now(timezone.utc)
    return due if due > now else now


def _compute_next_alert_time():
    """Same reasoning as _compute_next_scrape_time() -- don't fire the
    alert job immediately on every deploy, schedule it for whenever it
    was actually next due based on the last recorded run."""
    if not db_users.accounts_enabled():
        return datetime.now() + timedelta(days=1)
    try:
        last = db_users.last_alert_run()
    except Exception:
        return datetime.now() + timedelta(hours=1)
    if last is None:
        return datetime.now()
    ran_at = last.get("ran_at")
    if ran_at is None:
        return datetime.now()
    if ran_at.tzinfo is None:
        ran_at = ran_at.replace(tzinfo=timezone.utc)
    due = ran_at + timedelta(hours=24)
    now = datetime.now(timezone.utc)
    return due if due > now else now


@app.route("/api/jobs")
def api_jobs():
    q = request.args.get("q", "")
    locations = request.args.getlist("location")  # repeated ?location=a&location=b, multi-select
    location_groups = request.args.getlist("location_group")  # canonical "Remote (US)" etc. chips
    days = request.args.get("days", "")
    # Repeated ?department=a&department=b, multi-select -- same convention
    # as `location` above. Single-value callers (old saved searches, direct
    # API use) still work: db.search_jobs() falls back to the first/only
    # value when only one is sent.
    departments = request.args.getlist("department")
    department = departments[0] if departments else ""
    commitment = request.args.get("commitment", "")
    sort = request.args.get("sort", db.DEFAULT_SORT)
    resume_title_terms = [t.lower() for t in request.args.getlist("resume_title_term") if t.strip()]
    resume_skill_terms = [t.lower() for t in request.args.getlist("resume_skill_term") if t.strip()]
    resume_us_based = request.args.get("resume_us_based") == "1"
    resume_metro_terms = [t.lower() for t in request.args.getlist("resume_metro_term") if t.strip()]

    def _int_or_none(name):
        v = request.args.get(name, "")
        return int(v) if v.strip().lstrip("-").isdigit() else None

    salary_min = _int_or_none("salary_min")
    salary_max = _int_or_none("salary_max")
    yoe_min = _int_or_none("yoe_min")
    yoe_max = _int_or_none("yoe_max")
    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = min(100, max(1, int(request.args.get("per_page", 25) or 25)))

    days_val = int(days) if days.strip().isdigit() else None
    try:
        jobs, total = db.search_jobs(query=q, locations=locations, location_groups=location_groups,
                                      days=days_val, departments=departments,
                                      commitment=commitment, sort=sort,
                                      resume_title_terms=resume_title_terms,
                                      resume_skill_terms=resume_skill_terms,
                                      resume_us_based=resume_us_based,
                                      resume_metro_terms=resume_metro_terms,
                                      salary_min=salary_min, salary_max=salary_max,
                                      yoe_min=yoe_min, yoe_max=yoe_max,
                                      page=page, per_page=per_page)
    except Exception as e:
        return jsonify({"error": f"Couldn't parse that search: {e}"}), 400

    # Badge each row with whether the logged-in user's already applied --
    # one query for the whole page's worth of URLs, not one per card.
    # Silently skipped (no badges, no error) if accounts aren't configured
    # or nobody's logged in, so this never breaks plain anonymous search.
    if "user_id" in session and db_users.accounts_enabled():
        try:
            applied_urls = db_users.list_applied_job_urls(session["user_id"])
        except Exception:
            applied_urls = set()  # accounts DB hiccup shouldn't break search results
        for j in jobs:
            j["applied"] = j.get("url") in applied_urls
    else:
        for j in jobs:
            j["applied"] = False

    # detail_path -- the job's own /jobs/<id>-<slug> page (see _job_path()
    # below and render_job_page()). Added here so app.js's job cards can
    # link to it directly instead of that page only ever being reachable
    # via the sitemap; an internal link from a page Google already crawls
    # (this search results grid) is worth far more to crawl priority than
    # a sitemap entry alone. `.get("job_id")` rather than `["job_id"]` is
    # just defensive -- every row has one after db.py's backfill, but a
    # missing/falsy id (should never happen) degrades to no link instead
    # of a 500.
    for j in jobs:
        if j.get("job_id"):
            j["detail_path"] = _job_path(j["job_id"], j["company"], j["title"])

    return jsonify({
        "jobs": jobs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
    })


@app.route("/api/facets")
def api_facets():
    salary_lo, salary_hi = db.salary_bounds()
    yoe_lo, yoe_hi = db.years_bounds()
    return jsonify({
        "departments": db.distinct_facet_values("department", limit=40),
        "commitments": db.distinct_facet_values("commitment", limit=10),
        # Used to size the min/max endpoints of the salary + years-of-
        # experience dual-range sliders on the frontend -- see
        # db.salary_bounds()/years_bounds() for how these are derived.
        "salary_bounds": {"min": salary_lo, "max": salary_hi},
        "yoe_bounds": {"min": yoe_lo, "max": yoe_hi},
    })


@app.route("/api/locations")
def api_locations():
    q = request.args.get("q", "")
    return jsonify({"locations": db.distinct_locations(prefix=q, limit=20)})


@app.route("/api/location-groups")
def api_location_groups():
    """Canonical 'Remote (US)' / 'Remote (Canada)' / etc. options, pinned at
    the top of the location dropdown alongside raw per-company location
    strings — see location_groups.py for why these exist."""
    from location_groups import LOCATION_GROUPS
    return jsonify({"groups": [{"key": k, "label": v["label"]} for k, v in LOCATION_GROUPS.items()]})


@app.route("/api/metro-cities")
def api_metro_cities():
    """City/state pairs for the ~68 major US metros curated in
    metro_areas.py (already used server-side to expand a resume's home
    city into nearby suburbs -- see resume_parser.py). Reused here purely
    for the (city, state) *keys*, not the nearby-city lists, so Hunter
    (static/app.js) can recognize a bare metro name typed in chat --
    "san francisco" -- as "San Francisco, CA" without requiring the state
    to be spelled out. One shared list rather than a second hardcoded copy
    of city names in the frontend, so the two stay in sync automatically."""
    from metro_areas import METRO_AREAS
    cities = [
        {"city": city.title(), "state": state.upper()}
        for (city, state) in METRO_AREAS.keys()
    ]
    return jsonify({"cities": cities})


@app.route("/api/site-config")
def api_site_config():
    """Public, non-account config the frontend needs on every page load:
    the GA4 measurement ID (safe to expose; it's a public tracking ID, not
    a secret) and whether the contact form is wired up to actually send
    email. Each flag being false means app.js skips that feature entirely
    -- same graceful-degradation pattern as Turnstile/Resend/password-
    reset. (The guided search wizard/chat widget needs no entry here --
    it's a purely client-side scripted flow, not an external API call.)

    Deliberately does NOT include CONTACT_EMAIL. An earlier version of
    this endpoint returned it so the contact page could fall back to a
    mailto: link when the form itself wasn't configured, but that put the
    operator's real inbox address in plain text in the page and in the
    API response -- a real complaint, since there's no reason a visitor
    ever needs to see the destination address. send_contact_email() runs
    entirely server-side and is the only thing that ever needs
    CONTACT_EMAIL; the browser doesn't, so it doesn't get it."""
    return jsonify({
        "ga_measurement_id": GA_MEASUREMENT_ID,
        "contact_enabled": contact_enabled(),
    })


@app.route("/api/contact", methods=["POST"])
@limiter.limit("3 per hour")
def api_contact():
    if not contact_enabled():
        # Not 503 -- see the longer comment further down this same function
        # for why: Cloudflare (sitting in front of this deployment) swaps
        # any 5xx origin response for its own generic error page instead
        # of passing the real JSON body through. The frontend here doesn't
        # branch on status code (just res.ok/data.ok, same as everywhere
        # else), so there's no reason to risk a 5xx at all.
        return jsonify({"ok": False, "message": "The contact form isn't set up on this deployment yet."})

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if reason not in CONTACT_REASONS:
        # Anyone hitting the API directly (not through the dropdown) gets
        # bucketed into "Other" rather than rejected -- the reason is a
        # triage label for the inbox, not something worth 400-ing over.
        reason = "Other"
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    message = (data.get("message") or "").strip()

    if not name:
        return jsonify({"ok": False, "message": "Please enter your name."}), 400
    if not EMAIL_RE.match(email):
        return jsonify({"ok": False, "message": "That doesn't look like a valid email address."}), 400
    if not message:
        return jsonify({"ok": False, "message": "Please enter a message."}), 400
    if len(message) > MAX_CONTACT_MESSAGE_LEN:
        return jsonify({"ok": False, "message": f"That message is too long (max {MAX_CONTACT_MESSAGE_LEN} characters)."}), 400

    if not send_contact_email(reason, name, email, message):
        # Real failure reason (bad RESEND_API_KEY, Resend sandbox
        # restriction, etc.) only ever shows up in server logs -- see
        # send_contact_email()'s own logging -- but unlike login/forgot-
        # password there's no enumeration risk here, so it's fine to be
        # honest with the sender that it didn't go through.
        #
        # Deliberately 200, not 502/503/504 -- this site sits behind
        # Cloudflare, which by default swaps *any* 5xx origin response for
        # its own generic "error code: 5xx" plain-text interstitial rather
        # than passing the origin's body through. Confirmed live: this
        # branch used to return 502 with a real JSON body -- gunicorn's own
        # access log showed the correct 502 status and byte count leaving
        # the app -- but curl (and the browser) only ever received
        # Cloudflare's substituted plain-text page instead, which broke
        # the frontend's `await res.json()` and surfaced as the generic
        # "Something went wrong" catch-all instead of this actual message.
        # The 400s above are unaffected (Cloudflare only intercepts 5xx),
        # so this is the one response in this route that needed a non-5xx
        # status purely to survive the proxy sitting in front of it.
        return jsonify({"ok": False, "message": "Couldn't send that right now -- try again in a moment."})

    return jsonify({"ok": True, "message": "Thanks -- message sent. I'll get back to you soon."})


MAX_COMPANY_NAME_LEN = 200
MAX_CAREERS_URL_LEN = 500
MAX_COMPANY_REQUEST_NOTE_LEN = 1000


@app.route("/api/request-company", methods=["POST"])
@limiter.limit("10 per hour")
def api_request_company():
    """Public "request a company" form -- structured (company name +
    optional careers URL) rather than free text, specifically so these can
    be verified and batch-added faster than parsing prose out of contact-
    form submissions (see CONTACT_REASONS' existing "Add my company's job
    board" option, which still works fine for anyone who finds the contact
    page first, but this is the faster, more scalable path). Stored in
    db.py's SQLite job cache (see company_requests table) rather than the
    Postgres accounts db -- see create_company_request()'s docstring."""
    data = request.get_json(silent=True) or {}
    company_name = (data.get("company_name") or "").strip()
    careers_url = (data.get("careers_url") or "").strip()
    requester_email = (data.get("requester_email") or "").strip().lower()
    note = (data.get("note") or "").strip()

    if not company_name:
        return jsonify({"ok": False, "message": "Please enter the company name."}), 400
    if len(company_name) > MAX_COMPANY_NAME_LEN:
        return jsonify({"ok": False, "message": "That company name is too long."}), 400
    if careers_url and len(careers_url) > MAX_CAREERS_URL_LEN:
        return jsonify({"ok": False, "message": "That URL is too long."}), 400
    if careers_url and not re.match(r"^https?://", careers_url, re.IGNORECASE):
        return jsonify({"ok": False, "message": "Careers URL should start with http:// or https://"}), 400
    if requester_email and not EMAIL_RE.match(requester_email):
        return jsonify({"ok": False, "message": "That doesn't look like a valid email address."}), 400
    if len(note) > MAX_COMPANY_REQUEST_NOTE_LEN:
        return jsonify({"ok": False, "message": f"That note is too long (max {MAX_COMPANY_REQUEST_NOTE_LEN} characters)."}), 400

    db.create_company_request(company_name, careers_url, requester_email, note)

    # Best-effort email notification -- same "never let this block or fail
    # the visible response" treatment as everywhere else Resend is used.
    # Not the only way these surface (see /api/admin/company-requests),
    # so a failed/disabled send here just means checking the admin list
    # instead of getting pinged immediately.
    if RESEND_API_KEY and CONTACT_EMAIL:
        safe_name = html.escape(company_name)
        safe_url = html.escape(careers_url) if careers_url else "(not given)"
        safe_email = html.escape(requester_email) if requester_email else "(not given)"
        safe_note = html.escape(note).replace("\n", "<br>") if note else "(none)"
        payload = json.dumps({
            "from": RESEND_FROM_EMAIL,
            "to": [CONTACT_EMAIL],
            "subject": f"Skip The Boards: company request — {company_name}",
            "html": (
                f"<p><strong>Company:</strong> {safe_name}</p>"
                f"<p><strong>Careers URL:</strong> {safe_url}</p>"
                f"<p><strong>Requester email:</strong> {safe_email}</p>"
                f"<p><strong>Note:</strong> {safe_note}</p>"
            ),
        }).encode("utf-8")
        _post_to_resend(payload)

    return jsonify({"ok": True, "message": "Thanks -- I'll take a look and add it if I can verify a live board."})


JOB_FLAG_REASONS = ("Link doesn't work", "Listing looks closed/filled", "Wrong info", "Other")
MAX_JOB_FLAG_NOTE_LEN = 1000


@app.route("/api/flag-job", methods=["POST"])
@limiter.limit("20 per hour")
def api_flag_job():
    """Public "report a problem" button on job detail pages (see
    render_job_page()). Same reasoning as /api/request-company for
    storing in db.py's SQLite job cache rather than the Postgres accounts
    db, and for snapshotting job_title/company as plain text rather than
    joining against the live `jobs` row -- see create_job_flag()'s
    docstring."""
    data = request.get_json(silent=True) or {}
    job_url = (data.get("job_url") or "").strip()
    job_title = (data.get("job_title") or "").strip()
    company = (data.get("company") or "").strip()
    reason = (data.get("reason") or "").strip()
    note = (data.get("note") or "").strip()
    reporter_email = (data.get("reporter_email") or "").strip().lower()

    if not job_url:
        return jsonify({"ok": False, "message": "Missing job_url."}), 400
    if reason not in JOB_FLAG_REASONS:
        reason = "Other"  # same non-400 bucket-into-Other treatment as api_contact's reason field
    if len(note) > MAX_JOB_FLAG_NOTE_LEN:
        return jsonify({"ok": False, "message": f"That note is too long (max {MAX_JOB_FLAG_NOTE_LEN} characters)."}), 400
    if reporter_email and not EMAIL_RE.match(reporter_email):
        return jsonify({"ok": False, "message": "That doesn't look like a valid email address."}), 400

    db.create_job_flag(job_url, job_title, company, reason, note, reporter_email)
    return jsonify({"ok": True, "message": "Thanks for the heads up -- I'll take a look."})


JOB_FLAG_STATUSES = ("pending", "resolved", "dismissed")


@app.route("/api/admin/job-flags", methods=["GET"])
@accounts_required
@login_required
@admin_required
def api_admin_list_job_flags():
    status = request.args.get("status") or None
    if status and status not in JOB_FLAG_STATUSES:
        return jsonify({"ok": False, "message": "Invalid status filter."}), 400
    return jsonify({"ok": True, "flags": db.list_job_flags(status=status)})


@app.route("/api/admin/job-flags/<int:flag_id>", methods=["PATCH"])
@accounts_required
@login_required
@admin_required
def api_admin_update_job_flag(flag_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in JOB_FLAG_STATUSES:
        return jsonify({"ok": False, "message": f"status must be one of {JOB_FLAG_STATUSES}"}), 400
    updated = db.update_job_flag_status(flag_id, status)
    if not updated:
        return jsonify({"ok": False, "message": "Not found."}), 404
    return jsonify({"ok": True})


def _digest_unsub_token(email):
    """A short HMAC of the email, keyed on this process's SECRET_KEY --
    lets an unsubscribe link prove the clicker actually received that
    specific email (it's in the link Resend delivered) without needing a
    login or a separate token table like password resets use. Low
    stakes -- worst case of a forged token is someone unsubscribing an
    email they don't own from a marketing list, not an account takeover --
    so an unkeyed-per-token HMAC (rather than a stored, revocable token)
    is a reasonable, simpler tradeoff here specifically."""
    return hmac.new(app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key,
                     email.strip().lower().encode(), hashlib.sha256).hexdigest()[:16]


def _verify_digest_unsub_token(email, token):
    expected = _digest_unsub_token(email)
    return hmac.compare_digest(expected, (token or ""))


@app.route("/api/digest/subscribe", methods=["POST"])
@limiter.limit("10 per hour")
def api_digest_subscribe():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        return jsonify({"ok": False, "message": "That doesn't look like a valid email address."}), 400
    db.subscribe_to_digest(email)
    return jsonify({"ok": True, "message": "Subscribed -- you'll get the next weekly digest."})


@app.route("/unsubscribe")
def unsubscribe_page():
    email = (request.args.get("email") or "").strip().lower()
    token = request.args.get("token") or ""
    if email and _verify_digest_unsub_token(email, token):
        db.unsubscribe_from_digest(email)
        message = "You're unsubscribed from the weekly digest. Sorry to see you go."
    else:
        message = "That unsubscribe link looks invalid or expired."
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="robots" content="noindex" />
<title>Unsubscribe — Skip The Boards</title>
<link rel="stylesheet" href="/style.css?v=23" />
</head>
<body>
  <main class="content-page">
    <h1>Unsubscribe</h1>
    <p class="content-page-intro">{html.escape(message)}</p>
    <p><a href="/">← Back to Skip The Boards</a></p>
  </main>
</body>
</html>
"""
    return Response(body, mimetype="text/html")


@app.route("/mcp", methods=["POST"])
@limiter.limit("30 per minute")
def mcp_endpoint():
    """The MCP (Model Context Protocol) endpoint -- lets an AI agent call
    this site's job search directly, without a person driving a browser.
    See mcp_server.py for the actual protocol logic and the reasoning
    behind targeting protocol version 2025-06-18 rather than the newest
    2026-07-28 revision.

    This route owns only the HTTP mechanics (parsing, status codes, rate
    limiting); mcp_server.handle_request() owns everything protocol-
    specific. Rate limit is per-IP via the same flask_limiter instance
    every other rate-limited route in this file uses -- 30/minute is
    generous for a single agent's back-and-forth (each user turn is
    usually one or two tool calls) while still bounding worst-case load
    on the shared SQLite file from a misbehaving or abusive client.

    No `Origin` validation here unlike the spec's guidance for locally-
    bound servers: that check exists to prevent DNS-rebinding attacks
    against a server listening on localhost, which doesn't apply to a
    public, already-internet-facing deployment like this one -- this
    endpoint is meant to be reachable by any MCP client, the same way
    /api/jobs already is.
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": "Parse error: request body must be valid JSON"},
        }), 400

    try:
        result = mcp_server.handle_request(body)
    except ValueError as e:
        return jsonify({
            "jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None,
            "error": {"code": -32600, "message": f"Invalid Request: {e}"},
        }), 400

    if result is None:
        # A JSON-RPC notification (no `id`) -- the client isn't waiting
        # for a reply, so per the Streamable HTTP transport spec this is
        # a bare 202 Accepted with no body, not an empty JSON object.
        return "", 202

    return jsonify(result)


@app.route("/mcp", methods=["GET", "DELETE"])
def mcp_endpoint_unsupported():
    """This server doesn't support the old HTTP+SSE transport's GET-for-
    standalone-stream or session-termination-via-DELETE -- both are
    optional even under the older, widely-supported protocol revision
    this server targets (see mcp_server.py), and 405 is exactly the
    documented fallback response for a server that doesn't implement
    them."""
    return Response(status=405)


@app.route("/api/status")
def api_status():
    run = db.last_run()
    return jsonify({
        "total_jobs": db.total_jobs(),
        "total_companies": len(COMPANIES),
        "last_run": run,
        "scrape_in_progress": _scrape_in_progress,
        "scrape_progress": _scrape_progress if _scrape_in_progress else None,
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if _scrape_in_progress:
        return jsonify({"ok": False, "message": "A scrape is already running."}), 409
    threading.Thread(target=run_scrape_job, daemon=True).start()
    return jsonify({"ok": True, "message": "Scrape started."})


@app.route("/api/parse-resume", methods=["POST"])
def api_parse_resume():
    f = request.files.get("resume")
    if f is None or not f.filename:
        return jsonify({"ok": False, "message": "No file uploaded."}), 400

    name = f.filename.lower()
    if not name.endswith(ALLOWED_RESUME_EXT):
        return jsonify({"ok": False, "message": "Please upload a .pdf, .docx, or .txt file."}), 400

    data = f.read()
    if not data:
        return jsonify({"ok": False, "message": "That file looks empty."}), 400

    try:
        text = resume_parser.extract_text(data, name)
    except Exception as e:
        return jsonify({"ok": False, "message": f"Couldn't read that file: {e}"}), 400

    if not text.strip():
        return jsonify({"ok": False, "message": "Couldn't find any text in that file (is it a scanned image?)."}), 400

    title_terms, skill_terms, query = resume_parser.suggest_query(text)
    if not title_terms and not skill_terms:
        return jsonify({"ok": False, "message": "Couldn't find any obvious job titles or skills in that resume."}), 200

    location_terms, matched_city = resume_parser.extract_location(text)

    return jsonify({
        "ok": True,
        # Combined list, display-only (shown in the "Extracted: ..." status
        # line). Match scoring uses title_terms/skill_terms separately —
        # see db.py's _match_info for why the split matters.
        "terms": title_terms + skill_terms,
        "title_terms": title_terms,
        "skill_terms": skill_terms,
        "query": query,
        "location_terms": location_terms,
        # Always suggest Remote (US) alongside whatever city was found (or
        # even if none was) — per spec, virtually every search should default
        # to including remote roles, with the user free to deselect it.
        "location_groups": ["remote_us"],
        "matched_city": matched_city,
    })


# ---------------- accounts: auth ----------------

@app.route("/api/signup", methods=["POST"])
@limiter.limit("5 per hour")
@accounts_required
def api_signup():
    from werkzeug.security import generate_password_hash

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    turnstile_token = data.get("turnstile_token") or ""

    if not verify_turnstile(turnstile_token, request.remote_addr):
        return jsonify({"ok": False, "message": "Verification failed -- please try again."}), 400
    if not EMAIL_RE.match(email):
        return jsonify({"ok": False, "message": "That doesn't look like a valid email address."}), 400
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({"ok": False, "message": f"Password needs to be at least {MIN_PASSWORD_LEN} characters."}), 400

    try:
        user = db_users.create_user(email, generate_password_hash(password))
    except Exception as e:
        # psycopg2's UniqueViolation (email already registered) surfaces
        # here as some flavor of IntegrityError depending on driver/
        # transaction state -- checked by message substring rather than
        # importing the specific exception class, since the connection's
        # already been rolled back and closed by conn_ctx by this point.
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            return jsonify({"ok": False, "message": "That email's already registered. Try logging in instead."}), 409
        # Deliberately not 500 -- see the long comment on the equivalent
        # contact-form failure branch in api_contact() below for why: this
        # site sits behind Cloudflare, which swaps any 5xx origin response
        # for its own generic plain-text error page rather than passing
        # the origin's JSON body through, breaking the frontend's
        # `await res.json()` call. 200 with ok:false survives untouched.
        return jsonify({"ok": False, "message": f"Couldn't create that account: {e}"})

    session["user_id"] = user["id"]
    session.permanent = True
    return jsonify({"ok": True, "email": user["email"]})


@app.route("/api/login", methods=["POST"])
@limiter.limit("10 per minute")
@accounts_required
def api_login():
    from werkzeug.security import check_password_hash

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = db_users.get_user_by_email(email)
    # Deliberately the same generic error whether the email doesn't exist
    # or the password's wrong -- distinguishing the two in the response
    # would let someone probe which emails have accounts here at all.
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"ok": False, "message": "Incorrect email or password."}), 401

    session["user_id"] = user["id"]
    session.permanent = True
    return jsonify({"ok": True, "email": user["email"]})


@app.route("/api/forgot-password", methods=["POST"])
@limiter.limit("3 per hour")
@accounts_required
def api_forgot_password():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    # Always the same response, whether or not the email is registered, the
    # format's invalid, password reset isn't configured, or the send itself
    # fails -- any difference here would let this endpoint be used to probe
    # which emails have accounts (same reasoning as api_login's generic
    # "incorrect email or password"). Real problems (bad RESEND_API_KEY,
    # Resend's sandbox domain restriction, etc.) only ever show up in
    # server logs, via send_password_reset_email()'s own logging.
    generic = jsonify({
        "ok": True,
        "message": "If that email has an account, a password reset link is on its way.",
    })

    if not EMAIL_RE.match(email) or not password_reset_enabled():
        return generic

    user = db_users.get_user_by_email(email)
    if user:
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_TTL_HOURS)
        db_users.create_password_reset_token(user["id"], token_hash, expires_at)
        base = APP_BASE_URL or request.url_root.rstrip("/")
        reset_link = f"{base}/reset-password?token={raw_token}"
        send_password_reset_email(user["email"], reset_link)

    return generic


@app.route("/api/reset-password", methods=["POST"])
@limiter.limit("10 per hour")
@accounts_required
def api_reset_password():
    from werkzeug.security import generate_password_hash

    data = request.get_json(silent=True) or {}
    token = data.get("token") or ""
    new_password = data.get("password") or ""

    if len(new_password) < MIN_PASSWORD_LEN:
        return jsonify({"ok": False, "message": f"Password needs to be at least {MIN_PASSWORD_LEN} characters."}), 400
    if not token:
        return jsonify({"ok": False, "message": "Missing or invalid reset link."}), 400

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    user_id = db_users.get_valid_reset_token(token_hash)
    if not user_id:
        return jsonify({"ok": False, "message": "This reset link is invalid or has expired. Request a new one."}), 400

    db_users.update_password(user_id, generate_password_hash(new_password))
    # Consumed only after the password's actually updated -- if update_password
    # somehow raised, the token stays valid for a retry instead of being
    # burned on a failed attempt.
    db_users.consume_reset_token(token_hash)
    return jsonify({"ok": True, "message": "Password updated. You can log in now."})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth-config")
def api_auth_config():
    """Public config the frontend needs before rendering the auth forms --
    the Turnstile *site* key (safe to expose; it's not the secret) and
    whether password reset is set up at all (so the frontend can hide the
    "Forgot password?" link entirely rather than show a dead-end form).
    Empty/false means that feature isn't configured on this deployment, and
    the frontend skips rendering it, matching the backend skipping the
    corresponding check (verify_turnstile(), api_forgot_password())."""
    return jsonify({
        "turnstile_site_key": TURNSTILE_SITE_KEY,
        "password_reset_enabled": password_reset_enabled(),
    })


@app.route("/api/me")
def api_me():
    if "user_id" not in session or not db_users.accounts_enabled():
        return jsonify({"ok": False})
    user = db_users.get_user_by_id(session["user_id"])
    if not user:
        # Account was deleted (or DB reset) out from under an active
        # session cookie -- clear it rather than leaving a session
        # pointing at a user_id that no longer exists.
        session.clear()
        return jsonify({"ok": False})
    return jsonify({"ok": True, "email": user["email"]})


# ---------------- admin dashboard ----------------

@app.route("/api/admin/stats")
@accounts_required
@login_required
@admin_required
def api_admin_stats():
    days = request.args.get("days", default=60, type=int)
    days = max(7, min(days, 365))  # clamp -- generate_series() with an
    # unbounded ?days= from the query string could otherwise be used to
    # force an expensive/huge series; there's no real use case above a year
    return jsonify({"ok": True, "stats": db_users.get_user_stats(days=days)})


COMPANY_REQUEST_STATUSES = ("pending", "added", "duplicate", "rejected")


@app.route("/api/admin/company-requests", methods=["GET"])
@accounts_required
@login_required
@admin_required
def api_admin_list_company_requests():
    status = request.args.get("status") or None
    if status and status not in COMPANY_REQUEST_STATUSES:
        return jsonify({"ok": False, "message": "Invalid status filter."}), 400
    return jsonify({"ok": True, "requests": db.list_company_requests(status=status)})


@app.route("/api/admin/company-requests/<int:request_id>", methods=["PATCH"])
@accounts_required
@login_required
@admin_required
def api_admin_update_company_request(request_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in COMPANY_REQUEST_STATUSES:
        return jsonify({"ok": False, "message": f"status must be one of {COMPANY_REQUEST_STATUSES}"}), 400
    updated = db.update_company_request_status(request_id, status)
    if not updated:
        return jsonify({"ok": False, "message": "Not found."}), 404
    return jsonify({"ok": True})


# ---------------- accounts: saved searches ----------------

@app.route("/api/saved-searches", methods=["GET"])
@accounts_required
@login_required
def api_list_saved_searches():
    searches = db_users.list_saved_searches(session["user_id"])
    for s in searches:
        s["params"] = json.loads(s.pop("params_json"))
    return jsonify({"searches": searches})


@app.route("/api/saved-searches", methods=["POST"])
@accounts_required
@login_required
def api_create_saved_search():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    params = data.get("params")
    # Defaults to True (opted in) -- the frontend's save-search flow doesn't
    # currently expose a way to uncheck this at save time, so every new
    # search starts with alerts on and the user can turn them off afterward
    # from the saved-searches list. Accepting it here anyway (rather than
    # hardcoding True in the route) keeps the door open for a future
    # "don't email me about this one" checkbox at save time without another
    # backend change.
    alerts_enabled = data.get("alerts_enabled", True)
    if not name:
        return jsonify({"ok": False, "message": "Give this search a name."}), 400
    if not isinstance(params, dict):
        return jsonify({"ok": False, "message": "Missing search parameters."}), 400
    search = db_users.create_saved_search(session["user_id"], name, json.dumps(params),
                                           alerts_enabled=bool(alerts_enabled))
    search["params"] = json.loads(search.pop("params_json"))
    return jsonify({"ok": True, "search": search})


@app.route("/api/saved-searches/<int:search_id>", methods=["DELETE"])
@accounts_required
@login_required
def api_delete_saved_search(search_id):
    deleted = db_users.delete_saved_search(session["user_id"], search_id)
    if not deleted:
        return jsonify({"ok": False, "message": "Not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/saved-searches/<int:search_id>", methods=["PATCH"])
@accounts_required
@login_required
def api_update_saved_search(search_id):
    """Currently only toggles alerts_enabled -- the one setting per saved
    search that's meant to be flipped after the fact without deleting and
    re-creating it. Extend here if saved searches ever get other editable
    fields (e.g. renaming)."""
    data = request.get_json(silent=True) or {}
    if "alerts_enabled" not in data:
        return jsonify({"ok": False, "message": "Nothing to update."}), 400
    updated = db_users.set_saved_search_alerts(session["user_id"], search_id, bool(data["alerts_enabled"]))
    if not updated:
        return jsonify({"ok": False, "message": "Not found."}), 404
    return jsonify({"ok": True})


# ---------------- accounts: applied jobs ----------------

@app.route("/api/applied-jobs", methods=["POST"])
@accounts_required
@login_required
def api_mark_applied():
    data = request.get_json(silent=True) or {}
    job_url = (data.get("job_url") or "").strip()
    if not job_url:
        return jsonify({"ok": False, "message": "Missing job_url."}), 400
    db_users.mark_applied(session["user_id"], job_url)
    return jsonify({"ok": True})


@app.route("/api/applied-jobs", methods=["DELETE"])
@accounts_required
@login_required
def api_unmark_applied():
    data = request.get_json(silent=True) or {}
    job_url = (data.get("job_url") or "").strip()
    if not job_url:
        return jsonify({"ok": False, "message": "Missing job_url."}), 400
    db_users.unmark_applied(session["user_id"], job_url)
    return jsonify({"ok": True})


@app.route("/api/applied-jobs/status", methods=["PATCH"])
@accounts_required
@login_required
def api_update_applied_status():
    """Moves a tracked application to a new pipeline stage (interviewing,
    offer, rejected, ghosted, withdrawn, or back to applied) -- the status
    dropdown on each row in the "My Applications" list. Separate from the
    POST/DELETE routes above (which only ever mean "track this" / "stop
    tracking this") so an accidental double-click can't wipe out a status
    someone's already set."""
    data = request.get_json(silent=True) or {}
    job_url = (data.get("job_url") or "").strip()
    status = (data.get("status") or "").strip().lower()
    if not job_url:
        return jsonify({"ok": False, "message": "Missing job_url."}), 400
    if status not in db_users.APPLICATION_STATUSES:
        return jsonify({"ok": False, "message": "Unknown status."}), 400
    updated = db_users.update_applied_status(session["user_id"], job_url, status)
    if not updated:
        return jsonify({"ok": False, "message": "That job isn't marked applied."}), 404
    return jsonify({"ok": True})


@app.route("/api/applied-jobs/full")
@accounts_required
@login_required
def api_list_applied_jobs_full():
    """Full job details for everything the user's marked applied, for the
    "My Applications" view -- joins Postgres's applied_jobs (URL + when)
    against the live SQLite job cache (title/company/location/etc.), since
    applied_jobs only ever stores the URL. A posting that's since closed
    and dropped out of the live dataset (see db.prune_stale) still shows
    up here with just its URL and applied date -- the point of this list
    is the user's own history, not "still-open postings," so a closed
    listing doesn't just vanish from their record."""
    applied = db_users.list_applied_jobs(session["user_id"])
    urls = [a["job_url"] for a in applied]
    jobs_by_url = db.get_jobs_by_urls(urls)
    results = []
    for a in applied:
        job = jobs_by_url.get(a["job_url"])
        applied_at = a["applied_at"].isoformat() if hasattr(a["applied_at"], "isoformat") else a["applied_at"]
        status = a.get("status") or "applied"
        if job:
            job = dict(job)
            job["applied_at"] = applied_at
            job["applied"] = True
            job["status"] = status
            results.append(job)
        else:
            # No longer in the live dataset -- still surface it, just
            # without the fields we no longer have.
            results.append({
                "url": a["job_url"],
                "title": None,
                "company": None,
                "applied_at": applied_at,
                "applied": True,
                "status": status,
                "delisted": True,
            })
    return jsonify({"jobs": results})


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"ok": False, "message": "Too many attempts -- please wait a bit and try again."}), 429


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/faq")
def faq_page():
    # A real standalone page (static/faq.html), not a modal over the
    # homepage -- separate URL, linkable, indexable, no JS required to
    # read it.
    return send_from_directory(STATIC_DIR, "faq.html")


@app.route("/contact")
def contact_page():
    # Same treatment as /faq -- a real page (static/contact.html) instead
    # of a modal, so it's linkable/indexable and doesn't need the homepage
    # loaded first. The page's own inline script hits /api/site-config
    # itself to decide whether to show the real form or a mailto: fallback.
    return send_from_directory(STATIC_DIR, "contact.html")


@app.route("/request-company")
def request_company_page():
    return send_from_directory(STATIC_DIR, "request-company.html")


@app.route("/about")
def about_page():
    return send_from_directory(STATIC_DIR, "about.html")


@app.route("/robots.txt")
def robots_txt():
    """A real robots.txt authored by this app -- until now there wasn't
    one at all, so Cloudflare was serving its own generic default
    "content signals" placeholder in front of it, which doesn't point
    crawlers at a sitemap or say anything about /admin or /api. Allows
    everything except the admin dashboard, the raw JSON API (nothing
    there is meant to be indexed as a page -- see /sitemap.xml for what
    actually should be), and the password-reset link (single-use token in
    the URL, never something a search result should point at)."""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "Disallow: /reset-password\n"
        "\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


JOB_SITEMAP_PAGE_SIZE = 10000  # sitemap protocol caps a single file at 50,000 URLs; well under that


@app.route("/sitemap.xml")
def sitemap_xml():
    """Sitemap INDEX, not a flat list -- this deployment has 100k+ live
    jobs, way past the sitemap protocol's 50,000-URL-per-file ceiling, so
    the actual URLs live in /sitemap-static.xml (the 4 public pages plus
    the 5 remote-region hub pages -- well under the URL ceiling, no need
    for their own file), /sitemap-companies.xml (one hub page per company
    with a current opening -- still just a few thousand rows even at this
    dataset's scale, comfortably one file), and however many
    /sitemap-jobs-<n>.xml pages of JOB_SITEMAP_PAGE_SIZE each it takes to
    cover every job currently in db.py -- computed fresh on every request
    from db.total_jobs(), so this always reflects however many jobs
    actually exist right now without needing a redeploy when that count
    changes."""
    total = db.total_jobs()
    num_job_pages = (total + JOB_SITEMAP_PAGE_SIZE - 1) // JOB_SITEMAP_PAGE_SIZE
    sitemaps = [
        f"{SITE_URL}/sitemap-static.xml", f"{SITE_URL}/sitemap-companies.xml",
        f"{SITE_URL}/sitemap-salary.xml",
    ]
    sitemaps += [f"{SITE_URL}/sitemap-jobs-{n}.xml" for n in range(1, num_job_pages + 1)]
    entries = "\n".join(f"  <sitemap><loc>{loc}</loc></sitemap>" for loc in sitemaps)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n"
        "</sitemapindex>\n"
    )
    return Response(xml, mimetype="application/xml")


@app.route("/sitemap-static.xml")
def sitemap_static_xml():
    """The original 4-page sitemap (home, about, faq, contact), now also
    carrying the 5 remote-region hub pages (/jobs/remote/<group> -- see
    REMOTE_HUB_GROUPS below) -- only 9 URLs total, nowhere near needing
    their own dedicated sitemap file the way jobs and company hubs do.
    Moved out of /sitemap.xml itself once that became a sitemap index
    (see above) rather than a flat file. Deliberately doesn't include
    /admin (noindex already, see its own <meta> tag) or account-only
    pages."""
    pages = [
        (f"{SITE_URL}/", "daily", "1.0"),
        (f"{SITE_URL}/about", "monthly", "0.5"),
        (f"{SITE_URL}/faq", "monthly", "0.5"),
        (f"{SITE_URL}/contact", "monthly", "0.3"),
        (f"{SITE_URL}/request-company", "monthly", "0.3"),
        (f"{SITE_URL}/weekly-digest", "weekly", "0.6"),
    ]
    pages += [
        (f"{SITE_URL}/jobs/remote/{slug}", "daily", "0.7") for slug in REMOTE_HUB_GROUPS
    ]
    entries = "\n".join(
        f"  <url><loc>{loc}</loc><changefreq>{freq}</changefreq><priority>{pri}</priority></url>"
        for loc, freq, pri in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n"
        "</urlset>\n"
    )
    return Response(xml, mimetype="application/xml")


@app.route("/sitemap-jobs-<int:page>.xml")
def sitemap_jobs_xml(page):
    """One page of up to JOB_SITEMAP_PAGE_SIZE job URLs -- this is the
    actual point of today's SEO work: individual, crawlable, indexable
    pages for every live job, not just the 4 static ones. `page` is
    1-indexed to match /sitemap.xml's own listing. An out-of-range page
    (e.g. requested after the job count shrinks) just returns an empty
    <urlset> rather than a 404 -- a sitemap page briefly going empty
    between a prune and the next crawl is normal and harmless; a 404
    inside a sitemap a crawler already fetched once is more likely to
    read as an error worth flagging."""
    offset = (page - 1) * JOB_SITEMAP_PAGE_SIZE
    rows = db.list_jobs_for_sitemap(offset, JOB_SITEMAP_PAGE_SIZE)
    entries = []
    for r in rows:
        loc = f"{SITE_URL}{_job_path(r['job_id'], r['company'], r['title'])}"
        lastmod = (r.get("last_seen") or "")[:10]  # YYYY-MM-DD is all sitemaps need
        lastmod_tag = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        entries.append(f"  <url><loc>{loc}</loc>{lastmod_tag}<changefreq>daily</changefreq></url>")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries) + ("\n" if entries else "")
        + "</urlset>\n"
    )
    return Response(xml, mimetype="application/xml")


@app.route("/sitemap-companies.xml")
def sitemap_companies_xml():
    """One URL per company that currently has at least one live opening
    -- the /jobs/company/<slug> hub pages (see company_hub_page() further
    down). A separate file from sitemap-static.xml (unlike the 5 remote
    hub pages) because this one scales with the dataset -- a few thousand
    companies today, more as the scraper's company list grows -- while
    still comfortably under the single-file 50,000-URL cap, so unlike
    jobs it doesn't need paging across multiple files yet."""
    rows = db.list_companies_with_open_jobs()
    entries = []
    for r in rows:
        loc = f"{SITE_URL}/jobs/company/{_slugify(r['company'])}"
        entries.append(f"  <url><loc>{loc}</loc><changefreq>daily</changefreq></url>")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries) + ("\n" if entries else "")
        + "</urlset>\n"
    )
    return Response(xml, mimetype="application/xml")


def _slugify(text):
    """Lowercase, hyphenated, URL-safe version of a string -- used to
    build the human-readable part of a job's URL. Purely cosmetic/for
    SEO (search engines and people both read hyphenated words better
    than a bare hash); the actual database lookup in job_page() below
    never looks at this part of the path, only the job_id prefix, so
    this never needs to exactly round-trip and there's no risk of a
    weird title/company breaking the route."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:80] or "role"


def _job_path(job_id, company, title):
    """The canonical public path for a job -- /jobs/<job_id>-<slug>. Kept
    as one shared function so the sitemap, the search-results cards (a
    follow-up, not in this round), and the detail page's own <link
    rel="canonical"> can never disagree with each other about what a
    given job's URL is."""
    slug = _slugify(f"{title}-{company}")
    return f"/jobs/{job_id}-{slug}"


# schema.org's JobPosting.employmentType is a closed enum -- this app's
# own `commitment` field is free text pulled straight off each posting
# (see companies_data.py/scraper.py), so only values that map cleanly are
# translated; anything else just omits the field rather than guessing,
# since an employmentType Google can't reconcile with the visible page
# text is worse for a listing's credibility than not having one at all.
EMPLOYMENT_TYPE_MAP = {
    "full-time": "FULL_TIME", "full time": "FULL_TIME",
    "part-time": "PART_TIME", "part time": "PART_TIME",
    "contract": "CONTRACTOR", "contractor": "CONTRACTOR",
    "temporary": "TEMPORARY", "temp": "TEMPORARY",
    "internship": "INTERN", "intern": "INTERN",
    "volunteer": "VOLUNTEER",
    "per diem": "PER_DIEM",
}


def _time_ago(iso_string):
    """Server-side twin of app.js's timeAgo() -- same coarse
    minutes/hours/days/weeks/months buckets, so the "confirmed X ago"
    trust signal reads identically whether it's rendered here (this
    file's one server-rendered page, render_job_page()) or client-side on
    the search results grid's job cards. Returns "" for anything
    unparseable rather than raising -- this is a nice-to-have trust
    signal, not something that should ever 500 a job detail page."""
    if not iso_string:
        return ""
    try:
        then = datetime.fromisoformat(iso_string)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    diff = datetime.now(timezone.utc) - then
    mins = int(diff.total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} minute{'' if mins == 1 else 's'} ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = hours // 24
    if days < 7:
        return f"{days} day{'' if days == 1 else 's'} ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks} week{'' if weeks == 1 else 's'} ago"
    months = days // 30
    return f"{months} month{'' if months == 1 else 's'} ago"


def _job_salary_text(job):
    """Human-readable salary line for the detail page -- same "~$Xk" /
    "~$Xk-$Yk" convention app.js's formatSalary() uses for job cards, so
    a listing reads identically whether you saw it in search results
    first or landed straight on its own page from a search engine."""
    if not job.get("salary_min"):
        return ""
    lo, hi = job["salary_min"], job.get("salary_max") or job["salary_min"]
    fmt = lambda n: f"{n // 1000}k" if n % 1000 == 0 else f"{n / 1000:.0f}k"
    if lo == hi:
        return f"~${fmt(lo)}"
    return f"~${fmt(lo)}–${fmt(hi)}"


# ISO 3166-1 alpha-2 codes for the specific countries
# location_groups.EUROPE_COUNTRY_NAMES actually names -- excludes that
# set's two generic, non-country entries ("europe", "emea"), since
# neither has a single country code to assign.
_EUROPE_COUNTRY_ISO = {
    "germany": "DE", "france": "FR", "spain": "ES", "italy": "IT",
    "netherlands": "NL", "belgium": "BE", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "poland": "PL", "portugal": "PT",
    "austria": "AT", "switzerland": "CH", "ireland": "IE", "greece": "GR",
    "czech republic": "CZ", "hungary": "HU", "romania": "RO",
}


def _job_address(location):
    """Best-effort schema.org PostalAddress for a job's raw, free-text
    location string -- used by render_job_page()'s JobPosting JSON-LD.
    Reuses location_groups.py's own US/Canada/UK signal detection (the
    same classifiers already trusted for the "Remote (US)" etc. filter
    chips) rather than inventing a second, separate guess at what counts
    as a US location.

    Only ever asserts `addressCountry` when there's an actual textual
    signal for one in the raw string. This replaced an earlier version
    that hardcoded every job's addressCountry to "US" unconditionally --
    caught via Google's Rich Results Test on a real "Paris, France"
    posting, which was reporting the job as being in the US. Google's own
    structured-data guidelines treat a factually wrong field as worse
    than an absent optional one, so "no confident signal" here means
    omitting `addressCountry`/`addressRegion` entirely, not defaulting to
    a guess -- same "don't guess wrong" principle db.py's salary/YOE
    filters already follow for missing data."""
    if not location:
        return None
    addr = {"@type": "PostalAddress"}
    city_part = location.rpartition(",")[0].strip() if "," in location else location.strip()
    addr["addressLocality"] = city_part or location.strip()

    low = location.lower()
    if (
        US_WORD_RE.search(location)
        or US_STATE_ABBR_RE.search(location)
        or any(name in low for name in US_STATE_NAMES)
    ):
        addr["addressCountry"] = "US"
        abbr = US_STATE_ABBR_RE.search(location)
        if abbr:
            addr["addressRegion"] = abbr.group(1)
    elif CANADA_WORD_RE.search(location) or any(name in low for name in CANADA_PROVINCE_NAMES):
        addr["addressCountry"] = "CA"
    elif UK_WORD_RE.search(location):
        addr["addressCountry"] = "GB"
    else:
        for name, iso in _EUROPE_COUNTRY_ISO.items():
            if name in low:
                addr["addressCountry"] = iso
                break

    return addr


def _breadcrumb_jsonld(items):
    """BreadcrumbList JSON-LD, shared by every page that has one (job
    detail, company hub, remote hub) -- `items` is an ordered list of
    (name, url) tuples from the site root down to the current page.
    `url` should be None on the final entry: that's the "you are here"
    convention Google's own structured-data docs use, since a page
    linking to itself as its own breadcrumb target adds nothing. This is
    purely an eligibility signal for the breadcrumb trail search results
    sometimes show above a result's title/URL -- it doesn't change
    anything about the page's own visible navigation."""
    elements = []
    for i, (name, url) in enumerate(items, start=1):
        entry = {"@type": "ListItem", "position": i, "name": name}
        if url:
            entry["item"] = url
        elements.append(entry)
    schema = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": elements,
    }
    # .replace("</", "<\\/") -- a job/company name feeding into this (job
    # detail pages pass the job title as a breadcrumb label) could contain
    # the literal text "</script" and prematurely close the <script
    # type="application/ld+json"> block this gets embedded in. Still valid
    # JSON either way, "\/" is a standard escape for "/".
    return json.dumps(schema, ensure_ascii=False).replace("</", "<\\/")


def _js_str_literal(value):
    """json.dumps() a string for safe embedding inside an inline <script>
    block (not an HTML attribute, which is a different escaping context --
    see esc()/html.escape() for that). Guards against a job title/URL that
    happens to contain the literal text "</script" (data scraped from
    third-party career pages is not otherwise sanitized against this)
    prematurely closing the surrounding <script> tag -- json.dumps() alone
    doesn't escape forward slashes, so "<\\/script>" is the actual fix,
    not just defense in depth."""
    return json.dumps(str(value or "")).replace("</", "<\\/")


def render_job_page(job):
    """Builds the full standalone HTML document for a single job's public
    detail page -- title/meta description/canonical/OG/Twitter tags
    specific to *this* job (not the site-wide defaults every other page
    uses), a JobPosting JSON-LD block (the actual mechanism Google for
    Jobs runs on), and a human-readable version of the same content using
    the same .tag/.job-blurb/.tool-chip vocabulary job cards already use
    elsewhere on the site (see app.js's jobCard()) so this doesn't
    introduce a second, inconsistent visual language for a job listing.
    No Jinja templating anywhere else in this app (everything else is
    static files + a JSON API) -- this is the one server-rendered HTML
    page, and it's built the same explicit-string way contact.html's
    inline script or this file's robots.txt/sitemap routes already are,
    rather than introducing a whole new templating dependency for one
    route."""
    job_id = job["job_id"]
    title = job["title"]
    company = job["company"]
    location = job.get("location") or "Location not listed"
    url = job["url"]
    source = job.get("source") or ""
    department = job.get("department") or ""
    commitment = job.get("commitment") or ""
    blurb = job.get("blurb") or ""
    years_experience = job.get("years_experience") or ""
    tools = job.get("tools") or []
    posted = (job.get("posted") or "")[:10]
    last_seen = job.get("last_seen") or ""

    canonical_path = _job_path(job_id, company, title)
    canonical_url = f"{SITE_URL}{canonical_path}"
    page_title = f"{title} at {company} — Skip The Boards"
    salary_text = _job_salary_text(job)

    desc_bits = [f"{title} at {company}"]
    if location and location != "Location not listed":
        desc_bits.append(f"in {location}")
    description = " ".join(desc_bits) + ". "
    if blurb:
        description += blurb[:160]
    else:
        description += (
            f"Apply directly on {company}'s own career page via {source or 'their job board'} "
            "-- no account required, no re-typing your resume into a new portal."
        )
    description = description[:300]

    def esc(s):
        return html.escape(str(s or ""))

    tags_html = [f'<span class="tag tag-source">{esc(source)}</span>']
    if department:
        tags_html.append(f'<span class="tag tag-department">{esc(department)}</span>')
    if commitment:
        tags_html.append(f'<span class="tag tag-commitment">{esc(commitment)}</span>')
    if salary_text:
        tags_html.append(
            f'<span class="tag tag-salary" title="Best-effort estimate pulled from the listing, '
            f'not a guaranteed figure">{esc(salary_text)}</span>'
        )
    # Trust signal: when our own scraper last confirmed this exact listing
    # was still live -- see app.js's matching freshnessBadge on search
    # result cards for the full reasoning (distinct from "posted", which
    # never changes even once a listing's gone stale).
    last_seen_ago = _time_ago(last_seen)
    if last_seen_ago:
        tags_html.append(
            f'<span class="tag tag-freshness" title="Our scraper last confirmed this listing was '
            f'still live on {esc(company)}\'s own career page {esc(last_seen_ago)}">'
            f'✓ Confirmed {esc(last_seen_ago)}</span>'
        )

    blurb_html = ""
    if blurb or years_experience:
        yoe_html = f'<span class="yoe-badge">{esc(years_experience)} YOE</span> ' if years_experience else ""
        blurb_html = f'<div class="job-blurb job-detail-blurb">{yoe_html}{esc(blurb)}</div>'

    tools_html = ""
    if tools:
        chips = "".join(f'<span class="tool-chip">{esc(t)}</span>' for t in tools)
        tools_html = f'<div class="job-tools"><span class="tools-icon" aria-hidden="true">\U0001f527</span>{chips}</div>'

    # ---- JobPosting JSON-LD ----
    json_ld = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": title,
        "description": html.escape(blurb) if blurb else html.escape(description),
        "hiringOrganization": {"@type": "Organization", "name": company},
        "url": canonical_url,
        "directApply": False,
    }
    if posted:
        json_ld["datePosted"] = posted
    # Best-effort validThrough: this app itself prunes a listing after
    # JOB_STALE_DAYS of not seeing it again during a scrape (see
    # db.prune_stale via run_scrape_job), so "the day this page is
    # expected to stop existing" is a real, not guessed, date -- computed
    # from last_seen (the most recent confirmation the posting was still
    # live), not first_seen.
    if last_seen:
        try:
            last_seen_dt = datetime.fromisoformat(last_seen)
            valid_through = last_seen_dt + timedelta(days=JOB_STALE_DAYS)
            json_ld["validThrough"] = valid_through.date().isoformat()
        except (TypeError, ValueError):
            pass
    if location and location != "Location not listed":
        address = _job_address(location)
        if address:
            json_ld["jobLocation"] = {"@type": "Place", "address": address}
    employment_type = EMPLOYMENT_TYPE_MAP.get(commitment.strip().lower())
    if employment_type:
        json_ld["employmentType"] = employment_type
    if job.get("salary_min"):
        json_ld["baseSalary"] = {
            "@type": "MonetaryAmount",
            "currency": "USD",
            "value": {
                "@type": "QuantitativeValue",
                "minValue": job["salary_min"],
                "maxValue": job.get("salary_max") or job["salary_min"],
                "unitText": "YEAR",
            },
        }
    # .replace("</", "<\\/") guards against a job title/company/blurb --
    # scraped from third-party career pages, never sanitized against this
    # -- containing the literal text "</script" and prematurely closing
    # this <script type="application/ld+json"> block. Valid inside a JSON
    # string either way ("\/" is a standard JSON escape for "/"), so this
    # doesn't change what any JSON-LD consumer (e.g. Google) parses out of
    # it. See _js_str_literal()'s docstring for the same fix applied to
    # the job-flag button's inline script further down this function.
    json_ld_script = json.dumps(json_ld, ensure_ascii=False).replace("</", "<\\/")

    company_hub_path = f"/jobs/company/{_slugify(company)}"
    breadcrumb_script = _breadcrumb_jsonld([
        ("Home", f"{SITE_URL}/"),
        (f"{company} jobs", f"{SITE_URL}{company_hub_path}"),
        (title, None),
    ])

    # "More at this company" -- a handful of the same company's other
    # current openings, each linking to its own detail page, plus a link
    # to the full company hub. Purely an internal-linking/engagement
    # addition (more real links between real pages a crawler can follow,
    # more reason for a visitor to stay on-site after this one posting
    # isn't the fit) -- capped at 4 so it stays a sidebar-style list, not
    # a second full listing competing with the hub page itself.
    other_jobs = [j for j in db.jobs_for_company(company, limit=5) if j.get("job_id") != job_id][:4]
    more_jobs_html = ""
    if other_jobs:
        items = "".join(
            f'<li><a href="{esc(_job_path(j["job_id"], company, j["title"]))}">{esc(j["title"])}</a></li>'
            for j in other_jobs
        )
        more_jobs_html = f"""
      <div class="job-detail-more">
        <h2>More at {esc(company)}</h2>
        <ul class="job-detail-more-list">{items}</ul>
        <a class="job-detail-more-link" href="{esc(company_hub_path)}">See all {esc(company)} jobs →</a>
      </div>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<link rel="icon" href="/favicon.svg?v=23" type="image/svg+xml" />
<link rel="alternate icon" href="/favicon.ico?v=23" />
<link rel="apple-touch-icon" href="/apple-touch-icon.png?v=23" />
<title>{esc(page_title)}</title>
<meta name="description" content="{esc(description)}" />
<link rel="canonical" href="{esc(canonical_url)}" />
<meta property="og:type" content="website" />
<meta property="og:site_name" content="Skip The Boards" />
<meta property="og:url" content="{esc(canonical_url)}" />
<meta property="og:title" content="{esc(page_title)}" />
<meta property="og:description" content="{esc(description)}" />
<meta property="og:image" content="{esc(SITE_URL)}/og-image.png" />
<meta property="og:image:width" content="1200" />
<meta property="og:image:height" content="630" />
<meta name="twitter:card" content="summary_large_image" />
<meta name="twitter:title" content="{esc(page_title)}" />
<meta name="twitter:description" content="{esc(description)}" />
<meta name="twitter:image" content="{esc(SITE_URL)}/og-image.png" />
<script type="application/ld+json">{json_ld_script}</script>
<script type="application/ld+json">{breadcrumb_script}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/style.css?v=23" />
</head>
<body>
  <nav class="topnav">
    <div class="topnav-inner">
      <div class="brand">
        <a href="/" style="display:flex;align-items:center;gap:8px;text-decoration:none;color:inherit;">
          <span class="brand-mark">◆</span>
          <span class="brand-name">Skip The Boards</span>
        </a>
      </div>
      <div class="topnav-right">
        <nav class="nav-links">
          <a href="/">Home</a>
          <a href="/about">About</a>
          <a href="/faq">FAQ</a>
          <a href="/contact">Contact</a>
          <a href="/request-company">Request a company</a>
        </nav>
        <a class="brand-link" href="/">← Back to search</a>
      </div>
    </div>
  </nav>

  <main class="content-page job-detail-page">
    <div class="job-detail-card">
      <h1 class="job-detail-title">{esc(title)}</h1>
      <div class="job-detail-sub">{esc(company)} · {esc(location)}</div>
      <div class="job-detail-tags">{"".join(tags_html)}</div>
      {blurb_html}
      {tools_html}
      <div class="job-detail-apply-row">
        <a class="btn-primary" href="{esc(url)}" target="_blank" rel="noopener noreferrer">Apply on {esc(company)}'s site ↗</a>
        <a class="job-detail-back-link" href="/">← Back to search</a>
      </div>
      <p class="job-detail-fineprint">
        This posting is pulled directly from {esc(company)}'s own {esc(source) or "career"} page and applying
        happens on their site, not here. Posted{f" {esc(posted)}" if posted else ""}; Skip The Boards re-checks
        every company's board regularly and removes listings that disappear from the source.
      </p>
      <div class="job-flag-block">
        <button type="button" class="job-flag-toggle" id="job-flag-toggle">Report a problem with this listing</button>
        <form id="job-flag-form" class="job-flag-form hidden">
          <select id="job-flag-reason">
            {"".join(f'<option value="{esc(r)}">{esc(r)}</option>' for r in JOB_FLAG_REASONS)}
          </select>
          <textarea id="job-flag-note" maxlength="1000" rows="2" placeholder="Optional details"></textarea>
          <div class="job-flag-row">
            <button type="submit" class="row-action-btn">Submit</button>
            <span id="job-flag-status" class="job-flag-status"></span>
          </div>
        </form>
      </div>
    </div>{more_jobs_html}
  </main>

  <footer>
    <div class="footer-inner">
      <p class="footer-byline">
        <strong>Jared Edberg</strong> · <a href="https://www.linkedin.com/in/jared-edberg" target="_blank" rel="noopener">LinkedIn ↗</a>
      </p>
    </div>
  </footer>

  <script>
    (function() {{
      var toggle = document.getElementById("job-flag-toggle");
      var form = document.getElementById("job-flag-form");
      var statusEl = document.getElementById("job-flag-status");
      toggle.addEventListener("click", function() {{
        form.classList.toggle("hidden");
      }});
      form.addEventListener("submit", function(e) {{
        e.preventDefault();
        var reason = document.getElementById("job-flag-reason").value;
        var note = document.getElementById("job-flag-note").value.trim();
        var submitBtn = form.querySelector("button[type=submit]");
        submitBtn.disabled = true;
        statusEl.textContent = "";
        fetch("/api/flag-job", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            job_url: {_js_str_literal(url)},
            job_title: {_js_str_literal(title)},
            company: {_js_str_literal(company)},
            reason: reason,
            note: note,
          }}),
        }})
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            statusEl.textContent = data.message || "Thanks.";
            if (data.ok) {{
              form.querySelectorAll("select, textarea, button").forEach(function(el) {{ el.disabled = true; }});
            }} else {{
              submitBtn.disabled = false;
            }}
          }})
          .catch(function() {{
            statusEl.textContent = "Something went wrong. Try again.";
            submitBtn.disabled = false;
          }});
      }});
    }})();
  </script>
</body>
</html>
"""


@app.route("/jobs/<segment>")
def job_page(segment):
    """Public detail page for a single job -- see render_job_page()'s
    docstring for the full reasoning. `segment` is "<job_id>-<slug>";
    only the first 12 characters (job_id's fixed length, see
    db.compute_job_id) are ever actually used to look the job up, the
    rest is decorative. This means a bare /jobs/<job_id> (no slug at all)
    resolves exactly the same way -- useful for anyone who copies just
    the id -- without needing a second route or a redirect.

    Deliberately the default string converter, not <path:segment> -- a
    job slug never contains a literal "/" (see _slugify()), so there's no
    real path underneath one to capture, and using the plain converter
    means this route can only ever match exactly one path segment. That
    matters once /jobs/company/<slug> and /jobs/remote/<group> exist
    alongside it (see below): those are unambiguously distinct URL
    shapes from this route's point of view, rather than relying on
    Werkzeug's rule-specificity ordering to sort out an overlap between
    this route and a hypothetical /jobs/company/... it could otherwise
    have swallowed.

    A real 404 (not a soft-404 200) when the job_id doesn't match
    anything current -- almost always because the listing closed and
    db.prune_stale() already dropped it, not a broken link -- so search
    engines actually deindex the page over time instead of a stale
    listing lingering in results forever."""
    job_id = segment[:12]
    job = db.get_job_by_job_id(job_id)
    if job is None:
        body = """<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Role no longer available — Skip The Boards</title>
<meta name="robots" content="noindex" />
<link rel="stylesheet" href="/style.css?v=23" /></head>
<body><main class="content-page"><h1>This role isn't available anymore</h1>
<p class="content-page-intro">It's either been filled, taken down by the company, or the link's
just wrong. <a href="/">Search current openings instead →</a></p></main></body></html>"""
        return Response(body, status=404, mimetype="text/html")
    return Response(render_job_page(job), mimetype="text/html")


def _job_list_item_html(job):
    """One row of a hub page's job listing (company page, remote-region
    page -- see below). Deliberately simpler than app.js's jobCard(): no
    match badges (resume matching is a client-side-only feature, there's
    no resume to compare against on a server-rendered page) and the
    title links to this site's own /jobs/<id> page, not straight out to
    the ATS -- unlike the homepage's job cards, a hub page's whole job
    is to hand a crawler (or a person) off to the individual job page,
    not to be a second place to apply from."""
    def esc(s):
        return html.escape(str(s or ""))
    location = job.get("location") or "Location not listed"
    posted = (job.get("posted") or "")[:10]
    path = _job_path(job["job_id"], job["company"], job["title"])
    salary_text = _job_salary_text(job)
    tags = [f'<span class="tag tag-source">{esc(job.get("source"))}</span>']
    if job.get("department"):
        tags.append(f'<span class="tag tag-department">{esc(job["department"])}</span>')
    if salary_text:
        tags.append(f'<span class="tag tag-salary">{esc(salary_text)}</span>')
    return f"""
      <div class="job-card">
        <div class="job-card-header">
          <div class="job-header-text">
            <div class="job-title"><a href="{esc(path)}">{esc(job["title"])}</a></div>
            <div class="job-sub">{esc(job["company"])} · {esc(location)}</div>
          </div>
        </div>
        <div class="job-footer">
          <span class="job-posted">{esc(posted)}</span>
          {"".join(tags)}
        </div>
      </div>
    """


def render_hub_page(page_title, description, canonical_path, h1, intro_text, jobs, empty_message,
                     noindex=False, extra_intro_html=""):
    """Shared shell for the two kinds of listing/"hub" pages this site
    has -- /jobs/company/<slug> and /jobs/remote/<group> -- both are
    "here's a real, crawlable page for a whole category of jobs, each
    linking to its own page" rather than a single-job detail page (that's
    render_job_page()). No JobPosting structured data here on purpose --
    that schema describes one specific posting, not a list of them; a
    hub page's job is purely to be a well-linked entry point search
    engines and people can land on for a category-level query like
    "Acme Corp jobs" or "remote jobs," then click through to individual
    postings from there."""
    canonical_url = f"{SITE_URL}{canonical_path}"
    breadcrumb_script = _breadcrumb_jsonld([
        ("Home", f"{SITE_URL}/"),
        (h1, None),
    ])

    def esc(s):
        return html.escape(str(s or ""))

    if jobs:
        cards_html = "".join(_job_list_item_html(j) for j in jobs)
    else:
        cards_html = f'<p class="content-page-intro">{esc(empty_message)}</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<link rel="icon" href="/favicon.svg?v=23" type="image/svg+xml" />
<link rel="alternate icon" href="/favicon.ico?v=23" />
<link rel="apple-touch-icon" href="/apple-touch-icon.png?v=23" />
<title>{esc(page_title)}</title>
<meta name="description" content="{esc(description)}" />
<link rel="canonical" href="{esc(canonical_url)}" />
{'<meta name="robots" content="noindex" />' if noindex else ""}
<meta property="og:type" content="website" />
<meta property="og:site_name" content="Skip The Boards" />
<meta property="og:url" content="{esc(canonical_url)}" />
<meta property="og:title" content="{esc(page_title)}" />
<meta property="og:description" content="{esc(description)}" />
<meta property="og:image" content="{esc(SITE_URL)}/og-image.png" />
<meta name="twitter:card" content="summary_large_image" />
<meta name="twitter:title" content="{esc(page_title)}" />
<meta name="twitter:description" content="{esc(description)}" />
<meta name="twitter:image" content="{esc(SITE_URL)}/og-image.png" />
<script type="application/ld+json">{breadcrumb_script}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/style.css?v=23" />
</head>
<body>
  <nav class="topnav">
    <div class="topnav-inner">
      <div class="brand">
        <a href="/" style="display:flex;align-items:center;gap:8px;text-decoration:none;color:inherit;">
          <span class="brand-mark">◆</span>
          <span class="brand-name">Skip The Boards</span>
        </a>
      </div>
      <div class="topnav-right">
        <nav class="nav-links">
          <a href="/">Home</a>
          <a href="/about">About</a>
          <a href="/faq">FAQ</a>
          <a href="/contact">Contact</a>
          <a href="/request-company">Request a company</a>
        </nav>
        <a class="brand-link" href="/">← Back to search</a>
      </div>
    </div>
  </nav>

  <main class="content-page hub-page">
    <h1>{esc(h1)}</h1>
    <p class="content-page-intro">{esc(intro_text)}</p>
    {extra_intro_html}
    <div class="hub-job-list">
      {cards_html}
    </div>
  </main>

  <footer>
    <div class="footer-inner">
      <p class="footer-byline">
        <strong>Jared Edberg</strong> · <a href="https://www.linkedin.com/in/jared-edberg" target="_blank" rel="noopener">LinkedIn ↗</a>
      </p>
    </div>
  </footer>
</body>
</html>
"""


def _find_company_by_slug(slug):
    """Reverse-lookup from a URL slug back to the real company string
    stored in jobs.company. There's no separate slug column -- slugs are
    derived on the fly from whatever's actually in the data (see
    _slugify()), so this just slugifies every company with a current
    opening and checks for a match. Cheap enough at this dataset's scale
    (a few thousand distinct companies, not rows) to not need caching."""
    for row in db.list_companies_with_open_jobs():
        if _slugify(row["company"]) == slug:
            return row["company"]
    return None


@app.route("/jobs/company/<slug>")
def company_hub_page(slug):
    """One company's current openings, all linking to their own /jobs/<id>
    pages -- a real page for a "<Company> jobs" search to land on,
    something this site had nothing to offer for before today. 404s (not
    a soft-404) for a company with no current openings, same reasoning
    as job_page()'s 404 -- a company that's since had every role filled
    shouldn't have a lingering indexed page with nothing on it."""
    company = _find_company_by_slug(slug)
    if company is None:
        return Response(
            render_hub_page(
                "Company not found — Skip The Boards", "",
                f"/jobs/company/{slug}", "No current openings",
                "Either this company has no open roles right now, or the link's wrong.",
                [], "", noindex=True,
            ),
            status=404, mimetype="text/html",
        )
    jobs = db.jobs_for_company(company)
    page_title = f"{company} jobs — Skip The Boards"
    description = (
        f"{len(jobs)} open role{'s' if len(jobs) != 1 else ''} at {company}, pulled directly from "
        f"their own career page. Apply directly, no account required."
    )
    # Cross-link to the salary insight page for this company, but only if
    # it actually has enough disclosed-salary postings to show anything
    # (see db.salary_stats_for_company()'s minimum-sample-size cutoff) --
    # linking to a page that would just 404/show "not enough data" is
    # worse than not linking at all. Reads from the in-memory salary
    # cache (see _refresh_salary_cache_if_stale()), NOT a direct db call
    # -- this route is probably the single most-visited page type on the
    # whole site, and a real production outage happened because this
    # exact check used to run a full unindexed table scan on every view.
    _refresh_salary_cache_if_stale()
    salary_stats = _salary_cache["by_company_dict"].get(company)
    extra_html = (
        f'<p class="salary-back-link"><a href="/salary/company/{slug}">See {html.escape(company)} salary data →</a></p>'
        if salary_stats else ""
    )
    body = render_hub_page(
        page_title, description, f"/jobs/company/{slug}",
        f"{company} jobs", description, jobs,
        f"{company} doesn't have any current openings in this dataset.",
        extra_intro_html=extra_html,
    )
    return Response(body, mimetype="text/html")


# Canonical group -> (URL slug, human label) -- deliberately the same 5
# groups location_groups.py already defines and db.search_jobs() already
# knows how to filter by (the exact chips the homepage's location filter
# offers). Reusing that instead of inventing a fresh classification means
# these hub pages can never disagree with what "Remote (US)" etc. means
# anywhere else on the site.
REMOTE_HUB_GROUPS = {
    "us": ("remote_us", "Remote (US)"),
    "canada": ("remote_canada", "Remote (Canada)"),
    "uk": ("remote_uk", "Remote (UK)"),
    "europe": ("remote_europe", "Remote (Europe)"),
    "anywhere": ("remote_anywhere", "Remote (unspecified / global)"),
}


@app.route("/jobs/remote/<slug>")
def remote_hub_page(slug):
    """A hub page per remote-region group -- "remote jobs" and its
    variants are some of the highest-volume job-search queries there are,
    and until today this site had no page of its own to offer for any of
    them. Deliberately scoped to just these 5 curated groups rather than
    also building one per raw city/metro string: location data here is
    free text straight off each ATS (see FAQ's "best-effort" framing
    throughout), and building city-level hub pages well would mean
    solving the same city-name-normalization problem metro_areas.py only
    partially solves for resume matching -- worth doing right as its own
    follow-up, not worth rushing into a pile of near-duplicate thin pages
    for slightly different spellings of the same place."""
    group = REMOTE_HUB_GROUPS.get(slug)
    if group is None:
        return Response("Not found", status=404, mimetype="text/plain")
    group_key, label = group
    jobs, total = db.search_jobs(location_groups=[group_key], sort="newest", page=1, per_page=300)
    page_title = f"{label} jobs — Skip The Boards"
    description = (
        f"{total} {label.lower()} openings across Greenhouse, Lever, and Ashby career pages. "
        "Apply directly, no account required."
    )
    body = render_hub_page(
        page_title, description, f"/jobs/remote/{slug}",
        f"{label} jobs", description, jobs,
        f"No current {label.lower()} openings in this dataset.",
    )
    return Response(body, mimetype="text/html")


DIGEST_LOCATION_GROUPS = [g for g, _label in REMOTE_HUB_GROUPS.values()]
DIGEST_JOB_COUNT = 30


def _fetch_digest_jobs():
    """The last 7 days' remote postings, sorted salary-high-first --
    db.py's "salary_high" sort already puts NULL salaries last (see
    SORT_OPTIONS), so this naturally surfaces salary-confirmed listings
    ahead of ones with no parsed number, without needing a hard filter
    that would exclude real, relevant postings just because a number
    couldn't be extracted. Shared by both the public page and the
    scheduled email job so they're always showing the same list."""
    jobs, _total = db.search_jobs(
        location_groups=DIGEST_LOCATION_GROUPS, days=7, sort="salary_high",
        page=1, per_page=DIGEST_JOB_COUNT,
    )
    return jobs


@app.route("/weekly-digest")
def weekly_digest_page():
    jobs = _fetch_digest_jobs()
    description = (
        f"The {len(jobs)} highest-paying remote roles posted in the last 7 days across "
        "Greenhouse, Lever, and Ashby career pages. Updated live, refreshed weekly by email."
    )
    subscribe_html = """
      <div class="digest-subscribe" id="digest-subscribe">
        <form id="digest-subscribe-form">
          <input type="email" id="digest-email" placeholder="you@example.com" required />
          <button type="submit" class="btn-primary">Email me this weekly</button>
        </form>
        <div class="digest-subscribe-status" id="digest-subscribe-status"></div>
      </div>
      <script>
        document.getElementById("digest-subscribe-form").addEventListener("submit", function(e) {
          e.preventDefault();
          var email = document.getElementById("digest-email").value.trim();
          var statusEl = document.getElementById("digest-subscribe-status");
          var btn = e.target.querySelector("button[type=submit]");
          btn.disabled = true;
          fetch("/api/digest/subscribe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email: email }),
          })
            .then(function(r) { return r.json(); })
            .then(function(data) {
              statusEl.textContent = data.message || "";
              if (!data.ok) { btn.disabled = false; }
            })
            .catch(function() {
              statusEl.textContent = "Something went wrong. Try again.";
              btn.disabled = false;
            });
        });
      </script>
    """
    body = render_hub_page(
        "This week's best remote jobs — Skip The Boards",
        description, "/weekly-digest", "This week's best remote jobs",
        description, jobs,
        "No remote postings in the last 7 days matched this list -- check back soon.",
        extra_intro_html=subscribe_html,
    )
    return Response(body, mimetype="text/html")


def _fmt_k(n):
    """"$95k"-style formatting for a whole dollar figure -- same "~$Xk"
    convention _job_salary_text()/app.js's formatSalary() already use for
    individual job cards, so the salary insight pages read consistently
    with every other salary figure on the site."""
    n = int(round(n))
    return f"${n // 1000}k" if n % 1000 == 0 else f"${n / 1000:.1f}k"


def _salary_stats_html(stats, extra_note=""):
    """Shared little summary card for a single role/company salary page --
    median, range, and sample size, plus a one-line caveat that this is
    self-reported/scraped data, not a survey. Kept as one function so the
    role and company pages can never show this in two subtly different
    formats."""
    return f"""
      <div class="salary-stats-block">
        <div class="salary-stat"><span class="salary-stat-value">{_fmt_k(stats['median'])}</span><span class="salary-stat-label">Median posted salary</span></div>
        <div class="salary-stat"><span class="salary-stat-value">{_fmt_k(stats['low'])}–{_fmt_k(stats['high'])}</span><span class="salary-stat-label">Full range</span></div>
        <div class="salary-stat"><span class="salary-stat-value">{stats['count']}</span><span class="salary-stat-label">Postings with disclosed salary</span></div>
      </div>
      <p class="salary-caveat">Based on {stats['count']} current postings that disclosed a salary range on the employer's own career page -- not a survey, and not adjusted for level or location.{(' ' + extra_note) if extra_note else ''}</p>
    """


# ---------------------------------------------------------------------------
# Salary stats cache -- see db.salary_confirmed_jobs_by_role()'s docstring
# for the real production incident this exists to fix: every /salary*
# route originally called db.salary_stats_by_role()/salary_stats_by_company()/
# salary_stats_for_company()/jobs_for_role() directly, each doing its own
# full unindexed scan (plus a classify_role() call per row) over the
# WHOLE jobs table, on every single HTTP request. Against the tiny fake
# datasets used while building the feature this was instant; against a
# real ~100k-row production table, a single one of these requests could
# take long enough to tie up the app's worker process, backing up every
# other request behind it until Cloudflare gave up on the whole site
# (a 524, not a 500 -- the app never crashed, it just never got around to
# answering). The worst single offender was company_hub_page()'s new
# "see salary data" cross-link check, which ran this same expensive scan
# on every /jobs/company/<slug> view -- probably the single most-visited
# page type on the whole site.
#
# Fix: compute all of it once, cache it in memory, and serve every
# request from the cache until it's stale. This data doesn't need to be
# more current than "as of the last scrape cycle" anyway -- a salary
# median moving by one newly-scraped posting isn't something anyone
# needs to see within seconds of it happening.
_SALARY_CACHE_TTL = timedelta(minutes=20)
_salary_cache_lock = threading.Lock()
_salary_cache = {
    "computed_at": None,
    "by_role": [],          # list of stats dicts, in ROLE_DISPLAY_ORDER order
    "by_role_dict": {},     # label -> stats dict, for O(1) lookup
    "by_company": [],       # full list, sorted by median descending
    "by_company_dict": {},  # company -> stats dict, for O(1) lookup
    "jobs_by_role": {},     # label -> list of job dicts (already salary-sorted)
}


def _refresh_salary_cache_if_stale(force=False):
    """Recomputes the salary cache if it's empty or older than
    _SALARY_CACHE_TTL (or unconditionally if force=True, used to warm the
    cache once at startup -- see start_scheduler()'s caller). Double-
    checks staleness after acquiring the lock so two requests racing to
    refresh at the same moment don't both pay the (expensive, see above)
    recomputation cost -- the second one just finds the first one already
    did it and returns immediately."""
    now = datetime.now(timezone.utc)
    computed_at = _salary_cache["computed_at"]
    if not force and computed_at is not None and (now - computed_at) < _SALARY_CACHE_TTL:
        return
    with _salary_cache_lock:
        computed_at = _salary_cache["computed_at"]
        if not force and computed_at is not None and (datetime.now(timezone.utc) - computed_at) < _SALARY_CACHE_TTL:
            return
        by_role = db.salary_stats_by_role()
        # No `limit` here (a very high one instead) -- this cache holds
        # the FULL company list once; each caller slices whatever prefix
        # it needs (40 for the index page, 1000 for the sitemap) out of
        # the same cached list rather than the cache holding multiple
        # differently-limited copies.
        by_company = db.salary_stats_by_company(limit=1_000_000)
        jobs_by_role = db.salary_confirmed_jobs_by_role(limit_per_role=50)
        _salary_cache["by_role"] = by_role
        _salary_cache["by_role_dict"] = {s["label"]: s for s in by_role}
        _salary_cache["by_company"] = by_company
        _salary_cache["by_company_dict"] = {s["company"]: s for s in by_company}
        _salary_cache["jobs_by_role"] = jobs_by_role
        _salary_cache["computed_at"] = datetime.now(timezone.utc)


@app.route("/salary")
def salary_index_page():
    """The /salary hub -- one row per canonical role family and a
    top-paying-companies table, each linking to its own aggregate page.
    Real, crawlable "software engineer salary" / "<company> salary"
    style landing pages, built entirely from this site's own scraped
    salary data (see db.salary_stats_by_role()/salary_stats_by_company())
    -- nothing here is a survey or self-reported figure, just a rollup of
    real current postings."""
    _refresh_salary_cache_if_stale()
    role_stats = _salary_cache["by_role"]
    company_stats = _salary_cache["by_company"][:40]

    role_rows = "".join(
        f'<tr><td><a href="/salary/{_role_slug(s["label"])}">{html.escape(s["label"])}</a></td>'
        f'<td>{_fmt_k(s["median"])}</td><td>{_fmt_k(s["low"])}–{_fmt_k(s["high"])}</td><td>{s["count"]}</td></tr>'
        for s in role_stats
    ) or '<tr><td colspan="4">Not enough disclosed-salary data yet -- check back as more postings come in.</td></tr>'

    company_rows = "".join(
        f'<tr><td><a href="/salary/company/{_slugify(s["company"])}">{html.escape(s["company"])}</a></td>'
        f'<td>{_fmt_k(s["median"])}</td><td>{_fmt_k(s["low"])}–{_fmt_k(s["high"])}</td><td>{s["count"]}</td></tr>'
        for s in company_stats
    ) or '<tr><td colspan="4">Not enough disclosed-salary data yet -- check back as more postings come in.</td></tr>'

    extra_html = f"""
      <div class="salary-table-wrap">
        <h2 class="salary-table-heading">By role</h2>
        <table class="salary-table">
          <thead><tr><th>Role</th><th>Median</th><th>Range</th><th>Postings</th></tr></thead>
          <tbody>{role_rows}</tbody>
        </table>
      </div>
      <div class="salary-table-wrap">
        <h2 class="salary-table-heading">Highest-paying companies</h2>
        <table class="salary-table">
          <thead><tr><th>Company</th><th>Median</th><th>Range</th><th>Postings</th></tr></thead>
          <tbody>{company_rows}</tbody>
        </table>
      </div>
    """
    description = (
        "Median and range of disclosed salaries by role and by company, computed live from "
        "current Greenhouse/Lever/Ashby postings -- not a survey."
    )
    body = render_hub_page(
        "Salary Insights — Skip The Boards", description, "/salary",
        "Salary insights", description, [], "",
        extra_intro_html=extra_html,
    )
    return Response(body, mimetype="text/html")


def _role_slug(label):
    for slug, lbl in ROLE_LABELS_BY_SLUG.items():
        if lbl == label:
            return slug
    return ""


@app.route("/salary/<slug>")
def role_salary_page(slug):
    """One canonical role's aggregate salary stats plus its current
    highest-paying disclosed-salary postings. 404s (not a soft-404) for
    an unrecognized slug or a role with too little data to show yet --
    same "don't publish a thin/misleading page" reasoning as
    company_hub_page()'s no-current-openings 404."""
    label = ROLE_LABELS_BY_SLUG.get(slug)
    if label is None:
        return Response("Not found", status=404, mimetype="text/plain")
    _refresh_salary_cache_if_stale()
    stats = _salary_cache["by_role_dict"].get(label)
    if stats is None:
        return Response(
            render_hub_page(
                f"{label} salary — Skip The Boards", "", f"/salary/{slug}",
                f"{label} salary", "Not enough disclosed-salary postings for this role yet.",
                [], "", noindex=True,
            ),
            status=404, mimetype="text/html",
        )
    jobs = _salary_cache["jobs_by_role"].get(label, [])
    description = (
        f"Median disclosed salary for {label} roles is {_fmt_k(stats['median'])}, based on "
        f"{stats['count']} current postings across Greenhouse, Lever, and Ashby career pages."
    )
    extra_html = _salary_stats_html(stats) + '<p class="salary-back-link"><a href="/salary">← All roles &amp; companies</a></p>'
    body = render_hub_page(
        f"{label} salary — Skip The Boards", description, f"/salary/{slug}",
        f"{label} salary", description, jobs,
        f"No current {label} postings disclose a salary right now.",
        extra_intro_html=extra_html,
    )
    return Response(body, mimetype="text/html")


@app.route("/salary/company/<slug>")
def company_salary_page(slug):
    """One company's aggregate disclosed-salary stats plus its current
    highest-paying disclosed-salary postings. Reuses the exact same slug
    scheme as /jobs/company/<slug> (see _find_company_by_slug()) so the
    two pages can always cross-link to each other without any separate
    slug bookkeeping."""
    company = _find_company_by_slug(slug)
    if company is None:
        return Response("Not found", status=404, mimetype="text/plain")
    _refresh_salary_cache_if_stale()
    stats = _salary_cache["by_company_dict"].get(company)
    if stats is None:
        return Response(
            render_hub_page(
                f"{company} salary — Skip The Boards", "", f"/salary/company/{slug}",
                f"{company} salary", "Not enough disclosed-salary postings for this company yet.",
                [], "", noindex=True,
            ),
            status=404, mimetype="text/html",
        )
    jobs = sorted(
        (j for j in db.jobs_for_company(company, limit=500) if j.get("salary_min") and j.get("salary_max")),
        key=lambda j: j.get("salary_max") or 0, reverse=True,
    )
    description = (
        f"Median disclosed salary at {company} is {_fmt_k(stats['median'])}, based on "
        f"{stats['count']} current postings on their own career page."
    )
    extra_html = (
        _salary_stats_html(stats)
        + f'<p class="salary-back-link"><a href="/jobs/company/{slug}">See all {html.escape(company)} jobs →</a> · '
        f'<a href="/salary">All roles &amp; companies</a></p>'
    )
    body = render_hub_page(
        f"{company} salary — Skip The Boards", description, f"/salary/company/{slug}",
        f"{company} salary", description, jobs,
        f"No current {company} postings disclose a salary right now.",
        extra_intro_html=extra_html,
    )
    return Response(body, mimetype="text/html")


@app.route("/sitemap-salary.xml")
def sitemap_salary_xml():
    """/salary plus one URL per role page and per company-with-enough-
    salary-data page -- a separate file from sitemap-static.xml because,
    like sitemap-companies.xml, the company list here scales with the
    dataset rather than being a fixed handful of pages. Reads from the
    in-memory salary cache (see _refresh_salary_cache_if_stale()) rather
    than calling db.py directly -- a crawler hitting this sitemap was one
    of the likely triggers of a real production outage before this
    cache existed (see that function's docstring)."""
    _refresh_salary_cache_if_stale()
    pages = [(f"{SITE_URL}/salary", "weekly", "0.6")]
    for s in _salary_cache["by_role"]:
        pages.append((f"{SITE_URL}/salary/{_role_slug(s['label'])}", "weekly", "0.5"))
    for s in _salary_cache["by_company"][:1000]:
        pages.append((f"{SITE_URL}/salary/company/{_slugify(s['company'])}", "weekly", "0.4"))
    entries = "\n".join(
        f"  <url><loc>{loc}</loc><changefreq>{freq}</changefreq><priority>{pri}</priority></url>"
        for loc, freq, pri in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n"
        "</urlset>\n"
    )
    return Response(xml, mimetype="application/xml")


@app.route("/admin")
def admin_page():
    # No server-side auth check here -- see admin_required()'s docstring:
    # this just serves the static shell, which fetches /api/admin/stats on
    # load and shows its own "not authorized" state if that 401s/403s.
    return send_from_directory(STATIC_DIR, "admin.html")


@app.route("/reset-password")
def reset_password_page():
    # Same static/index.html as "/" -- this is a single-page app, so the
    # actual "page" here is just app.js noticing a `?token=...` query
    # param on load and opening the "set a new password" modal for it (see
    # checkForResetToken() in app.js). A dedicated Flask route exists only
    # so this URL (the one emailed to the user) resolves to something
    # instead of a 404 -- without it, Flask's static handler has no file
    # called "reset-password" to serve.
    return send_from_directory(STATIC_DIR, "index.html")


db.init_db()
if db_users.accounts_enabled():
    db_users.init_db()
else:
    print("[app] DATABASE_URL not set -- user accounts (saved searches, "
          "applied-job tracking) are disabled on this deployment. Search "
          "and resume matching work as normal. See README's 'User "
          "accounts' section to enable accounts.")
if db_users.accounts_enabled() and not password_reset_enabled():
    print("[app] RESEND_API_KEY not set -- password reset is disabled on "
          "this deployment (signup/login/saved searches/applied jobs all "
          "still work normally). See README's 'User accounts' section to "
          "enable it.")
if not contact_enabled():
    print("[app] RESEND_API_KEY and/or CONTACT_EMAIL not set -- the contact "
          "form is disabled on this deployment; the contact page shows a "
          "plain 'not available right now' message instead (no mailto: "
          "fallback -- see README's 'Contact form' section for why).")
_scheduler = start_scheduler()

# Warm the salary cache once at startup, in a background thread -- NOT
# synchronously here at module import time, since that would block every
# worker process from finishing boot on the exact same expensive
# full-table scan this cache exists to get off the request path (see
# _refresh_salary_cache_if_stale()'s docstring). A cold cache is already
# handled safely either way (the first request to a /salary* route just
# triggers the same refresh inline), this just makes that unlikely to be
# the first thing that happens after a deploy.
threading.Thread(target=_refresh_salary_cache_if_stale, kwargs={"force": True}, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
