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
import html
import json
import os
import re
import secrets
import threading
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

        _, stats = scrape_all(max_workers=MAX_WORKERS, progress_cb=progress_cb, batch_cb=batch_cb)
        removed = db.prune_stale(max_age_days=JOB_STALE_DAYS)
        finished = datetime.now(timezone.utc).isoformat()
        db.record_run(started, finished, "ok", stats, db.total_jobs())
        print(f"[scrape] done: {stats}, new={new_count}, pruned={removed}")
    except Exception as e:
        finished = datetime.now(timezone.utc).isoformat()
        db.record_run(started, finished, f"error: {e}", {}, db.total_jobs())
        print(f"[scrape] FAILED: {e}")
    finally:
        _scrape_in_progress = False
        _scrape_lock.release()


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
    if not name:
        return jsonify({"ok": False, "message": "Give this search a name."}), 400
    if not isinstance(params, dict):
        return jsonify({"ok": False, "message": "Missing search parameters."}), 400
    search = db_users.create_saved_search(session["user_id"], name, json.dumps(params))
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
    the actual URLs live in /sitemap-static.xml (the 4 public pages) and
    however many /sitemap-jobs-<n>.xml pages of JOB_SITEMAP_PAGE_SIZE
    each it takes to cover every job currently in db.py -- computed fresh
    on every request from db.total_jobs(), so this always reflects
    however many jobs actually exist right now without needing a
    redeploy when that count changes."""
    total = db.total_jobs()
    num_job_pages = (total + JOB_SITEMAP_PAGE_SIZE - 1) // JOB_SITEMAP_PAGE_SIZE
    sitemaps = [f"{SITE_URL}/sitemap-static.xml"]
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
    """The original 4-page sitemap (home, about, faq, contact) -- moved
    out of /sitemap.xml itself once that became a sitemap index (see
    above) rather than a flat file. Deliberately doesn't include /admin
    (noindex already, see its own <meta> tag) or account-only pages."""
    pages = [
        (f"{SITE_URL}/", "daily", "1.0"),
        (f"{SITE_URL}/about", "monthly", "0.5"),
        (f"{SITE_URL}/faq", "monthly", "0.5"),
        (f"{SITE_URL}/contact", "monthly", "0.3"),
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
        json_ld["jobLocation"] = {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": location, "addressCountry": "US"},
        }
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
    json_ld_script = json.dumps(json_ld, ensure_ascii=False)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<link rel="icon" href="/favicon.svg?v=16" type="image/svg+xml" />
<link rel="alternate icon" href="/favicon.ico?v=16" />
<link rel="apple-touch-icon" href="/apple-touch-icon.png?v=16" />
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/style.css?v=16" />
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


@app.route("/jobs/<path:segment>")
def job_page(segment):
    """Public detail page for a single job -- see render_job_page()'s
    docstring for the full reasoning. `segment` is "<job_id>-<slug>";
    only the first 12 characters (job_id's fixed length, see
    db.compute_job_id) are ever actually used to look the job up, the
    rest is decorative. This means a bare /jobs/<job_id> (no slug at all)
    resolves exactly the same way -- useful for anyone who copies just
    the id -- without needing a second route or a redirect.

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
<link rel="stylesheet" href="/style.css?v=16" /></head>
<body><main class="content-page"><h1>This role isn't available anymore</h1>
<p class="content-page-intro">It's either been filled, taken down by the company, or the link's
just wrong. <a href="/">Search current openings instead →</a></p></main></body></html>"""
        return Response(body, status=404, mimetype="text/html")
    return Response(render_job_page(job), mimetype="text/html")


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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
