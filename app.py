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
import json
import os
import re
import secrets
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.error import URLError

from flask import Flask, jsonify, request, send_from_directory, session
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
MAX_RESUME_BYTES = 8 * 1024 * 1024  # 8 MB
ALLOWED_RESUME_EXT = (".pdf", ".docx", ".txt")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Open Roles Finder <onboarding@resend.dev>")
# Used to build the link inside the reset email (e.g. "https://open-roles-
# finder.onrender.com") -- deliberately an explicit env var rather than
# inferred from request.url_root, since that can be wrong behind a proxy/
# load balancer. Falls back to request.url_root at send-time if unset,
# which is fine for local dev but should be set explicitly in production.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
RESET_TOKEN_TTL_HOURS = 1
# Optional GA4 traffic tracking -- off unless set, same graceful-
# degradation pattern as everything else here. Injected client-side (see
# GET /api/site-config + app.js's loadSiteConfig()) rather than baked into
# static/index.html directly, so the measurement ID isn't hardcoded into
# version control and can be changed via env var alone.
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "")

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
        removed = db.prune_stale(max_age_days=10)
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


def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(run_scrape_job, "interval", hours=SCRAPE_INTERVAL_HOURS, id="scrape",
                       next_run_time=datetime.now())
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


def send_password_reset_email(to_email, reset_link):
    """Sends the reset-link email via Resend's REST API -- plain urllib,
    same pattern as verify_turnstile() above, so this doesn't need to pull
    in an HTTP client dependency (or the `resend` package) for one call.
    Returns True/False; the caller always shows the same generic response
    to the browser regardless of the result (see api_forgot_password), so a
    delivery failure here only ever surfaces in the server logs, never as a
    signal to whoever's making the request about whether the email exists
    or whether sending succeeded."""
    payload = json.dumps({
        "from": RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": "Reset your Open Roles Finder password",
        "html": (
            "<p>Someone (hopefully you) asked to reset the password on your "
            "Open Roles Finder account.</p>"
            f'<p><a href="{reset_link}">Click here to set a new password</a>. '
            f"This link expires in {RESET_TOKEN_TTL_HOURS} hour"
            f"{'s' if RESET_TOKEN_TTL_HOURS != 1 else ''}.</p>"
            "<p>If you didn't request this, you can safely ignore this "
            "email -- your password hasn't been changed.</p>"
        ),
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {RESEND_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        # Broad except deliberately -- URLError/HTTPError/timeout/whatever
        # else, none of it should ever bubble up into the request handler
        # as a 500. The user still sees the same generic "check your email"
        # response either way (see api_forgot_password); this is purely for
        # the server operator to notice in logs (e.g. RESEND_API_KEY typo'd,
        # or Resend's sandbox restriction blocking delivery to a real user
        # because a custom domain hasn't been verified yet -- see README).
        print(f"[app] password reset email to {to_email} failed to send: {e}")
        return False


def login_required(f):
    """Route decorator -- returns 401 instead of running the view at all
    if there's no logged-in user. Applied to every saved-search/applied-
    job route below; /api/jobs itself stays open to everyone (accounts
    are optional, not a gate on searching)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Log in to use this feature."}), 401
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
            return jsonify({"error": "Accounts aren't set up on this deployment yet."}), 503
        return f(*args, **kwargs)
    return wrapper


@app.route("/api/jobs")
def api_jobs():
    q = request.args.get("q", "")
    locations = request.args.getlist("location")  # repeated ?location=a&location=b, multi-select
    location_groups = request.args.getlist("location_group")  # canonical "Remote (US)" etc. chips
    days = request.args.get("days", "")
    department = request.args.get("department", "")
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
                                      days=days_val, department=department,
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


@app.route("/api/site-config")
def api_site_config():
    """Public, non-account config the frontend needs on every page load --
    currently just the GA4 measurement ID (safe to expose; it's a public
    tracking ID, not a secret). Empty string means analytics isn't
    configured on this deployment, and app.js skips injecting gtag.js
    entirely -- same graceful-degradation pattern as Turnstile/Resend."""
    return jsonify({"ga_measurement_id": GA_MEASUREMENT_ID})


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
        return jsonify({"ok": False, "message": f"Couldn't create that account: {e}"}), 500

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
        if job:
            job = dict(job)
            job["applied_at"] = a["applied_at"].isoformat() if hasattr(a["applied_at"], "isoformat") else a["applied_at"]
            job["applied"] = True
            results.append(job)
        else:
            # No longer in the live dataset -- still surface it, just
            # without the fields we no longer have.
            results.append({
                "url": a["job_url"],
                "title": None,
                "company": None,
                "applied_at": a["applied_at"].isoformat() if hasattr(a["applied_at"], "isoformat") else a["applied_at"],
                "applied": True,
                "delisted": True,
            })
    return jsonify({"jobs": results})


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"ok": False, "message": "Too many attempts -- please wait a bit and try again."}), 429


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


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
_scheduler = start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
