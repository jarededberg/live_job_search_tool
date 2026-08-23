"""
app.py — Flask web app: serves the search UI + JSON API, and runs the
background scraper on a schedule.

Run locally:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:8000
"""

import os
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory

import db
from scraper import scrape_all
from companies_data import COMPANIES
import resume_parser

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")

SCRAPE_INTERVAL_HOURS = float(os.environ.get("SCRAPE_INTERVAL_HOURS", "8"))
MAX_WORKERS = int(os.environ.get("SCRAPE_MAX_WORKERS", "4"))
MAX_RESUME_BYTES = 8 * 1024 * 1024  # 8 MB
ALLOWED_RESUME_EXT = (".pdf", ".docx", ".txt")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = MAX_RESUME_BYTES

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
                                      page=page, per_page=per_page)
    except Exception as e:
        return jsonify({"error": f"Couldn't parse that search: {e}"}), 400
    return jsonify({
        "jobs": jobs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
    })


@app.route("/api/facets")
def api_facets():
    return jsonify({
        "departments": db.distinct_facet_values("department", limit=40),
        "commitments": db.distinct_facet_values("commitment", limit=10),
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


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


db.init_db()
_scheduler = start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
