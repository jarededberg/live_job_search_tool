"""
scraper.py — Job board scraper core.

Ported and simplified from Jared Edberg's original job_search.py. Fetches
current openings from ~4,300 companies' Greenhouse / Lever / Ashby boards
with no role or location filtering baked in — every job at every company
gets stored. Filtering by keyword/location/recency happens at query time
in app.py, so every visitor can search the same cached dataset their own
way.
"""

import json
import re
import threading
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError

from companies_data import COMPANIES

_LOCAL = threading.local()


def fetch(url, timeout=8):
    """GET a URL and parse the response as JSON. Returns None on any failure."""
    headers = {"User-Agent": "Mozilla/5.0 JobSearchWebApp/1.0"}
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except HTTPError as e:
        return None
    except Exception:
        return None


def parse_ts(ts):
    """Best-effort parse of a timestamp into an ISO date string (YYYY-MM-DD)."""
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        ts = str(ts).strip()
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
        try:
            return datetime.strptime(ts[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    except Exception:
        pass
    return None


def make_job(title, company, location, posted, url, source):
    return {
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "posted": posted,  # ISO date string or None
        "url": url,
        "source": source,
    }


def try_greenhouse(company, slug):
    data = fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if data is None:
        return None
    results = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        location = job.get("location", {}).get("name", "")
        job_url = job.get("absolute_url", "")
        posted = parse_ts(job.get("updated_at") or job.get("created_at"))
        if title and job_url:
            results.append(make_job(title, company, location, posted, job_url, "Greenhouse"))
    return results


def try_lever(company, slug):
    data = fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if data is None:
        return None
    if isinstance(data, dict):
        data = data.get("postings", [])
    if not isinstance(data, list):
        return None
    results = []
    for job in data:
        title = job.get("text", "")
        cats = job.get("categories", {}) or {}
        locs = cats.get("allLocations") or []
        location = locs[0] if locs else cats.get("location", "")
        job_url = job.get("hostedUrl", "")
        posted = parse_ts(job.get("createdAt"))
        if title and job_url:
            results.append(make_job(title, company, location, posted, job_url, "Lever"))
    return results


def try_ashby(company, slug):
    data = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if data is None:
        return None
    results = []
    for job in data.get("jobPostings", []):
        title = job.get("title", "")
        location = job.get("location", "") or job.get("locationName", "")
        path = job.get("jobPostingPath", "")
        job_url = f"https://jobs.ashbyhq.com/{slug}{path}" if path and not path.startswith("http") else path
        posted = parse_ts(job.get("publishedAt") or job.get("createdAt"))
        if title and job_url:
            results.append(make_job(title, company, location, posted, job_url, "Ashby"))
    return results


def search_company(name, slug):
    """Try Greenhouse -> Lever -> Ashby (and slug variants) for one company.
    Returns (jobs, platform, status) where status is 'found_jobs' | 'found_no_match' | 'not_found'.
    """
    variants = list(dict.fromkeys([slug, slug.replace("-", ""), slug.replace("-", "_")]))
    platforms = [(try_greenhouse, "Greenhouse"), (try_lever, "Lever"), (try_ashby, "Ashby")]
    first_confirmed = None
    for variant in variants:
        for fn, label in platforms:
            r = fn(name, variant)
            if r is None:
                continue
            if r:
                return r, label, "found_jobs"
            if first_confirmed is None:
                first_confirmed = label
    if first_confirmed:
        return [], first_confirmed, "found_no_match"
    return [], "not_found", "not_found"


def scrape_all(companies=None, max_workers=30, progress_cb=None):
    """Scrape every company in COMPANIES (or a provided subset). Returns a flat
    list of job dicts plus simple stats. Thread-pooled for speed."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    companies = companies if companies is not None else COMPANIES
    seen_slugs = set()
    clean = []
    for name, slug in companies:
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            clean.append((name, slug))

    all_jobs = []
    not_found = 0
    found_no_match = 0
    found = 0
    done = 0

    def _one(item):
        name, slug = item
        return name, slug, *search_company(name, slug)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_one, item): item for item in clean}
        for fut in as_completed(futures):
            done += 1
            try:
                name, slug, jobs, platform, status = fut.result()
            except Exception:
                not_found += 1
                if progress_cb:
                    progress_cb(done, len(clean))
                continue
            if status == "found_jobs":
                found += 1
                all_jobs.extend(jobs)
            elif status == "found_no_match":
                found_no_match += 1
            else:
                not_found += 1
            if progress_cb:
                progress_cb(done, len(clean))

    stats = {
        "companies_scanned": len(clean),
        "companies_with_jobs": found,
        "companies_found_no_openings": found_no_match,
        "companies_not_found": not_found,
        "jobs_scraped": len(all_jobs),
    }
    return all_jobs, stats
