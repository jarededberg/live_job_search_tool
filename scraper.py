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

import ijson

from companies_data import COMPANIES
from salary_extractor import strip_html, extract_salary

_LOCAL = threading.local()


def open_stream(url, timeout=10):
    """Open a URL for streaming (does NOT read the body). Returns the response
    object on success, or None if the URL isn't reachable at all (so callers
    can distinguish "board doesn't exist" from "board exists but is empty",
    same contract as fetch())."""
    headers = {"User-Agent": "Mozilla/5.0 JobSearchWebApp/1.0"}
    req = Request(url, headers=headers, method="GET")
    try:
        return urlopen(req, timeout=timeout)
    except HTTPError:
        return None
    except Exception:
        return None


def stream_items(resp, item_path):
    """Yield JSON items at `item_path` (e.g. 'item' for a top-level array,
    'jobs.item' for {"jobs": [...]}) from an open response, one at a time via
    ijson, without ever materializing the full response in memory at once.

    This matters specifically for Lever and Ashby: unlike Greenhouse, neither
    has an opt-out for the full HTML+plain-text job description on every
    posting (measured ~17-19KB/job on average), so a single large company's
    response can be several MB. Streaming means peak memory per request is
    roughly "one job's worth" instead of "the whole company's worth", which
    is what was still causing OOM restarts on Render even after the
    Greenhouse fix.
    """
    try:
        yield from ijson.items(resp, item_path)
    except Exception:
        return  # malformed / truncated mid-stream — return whatever we already yielded


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


def normalize_commitment(raw):
    """Collapse the many ATS-specific spellings of employment type into a
    small canonical set so the facet dropdown isn't a mess of near-duplicates."""
    r = (raw or "").strip().lower()
    if not r:
        return ""
    if "intern" in r:
        return "Internship"
    if "part" in r:
        return "Part-time"
    if "contract" in r or "temp" in r or "freelance" in r:
        return "Contract / Temporary"
    if "full" in r:
        return "Full-time"
    return "Other"


def make_job(title, company, location, posted, url, source, department="", commitment="",
             salary_min=None, salary_max=None):
    return {
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "posted": posted,  # ISO date string or None
        "url": url,
        "source": source,
        "department": (department or "").strip(),
        "commitment": normalize_commitment(commitment),
        "salary_min": salary_min,
        "salary_max": salary_max,
    }


def try_greenhouse(company, slug):
    # content=true IS passed here. That flag returns the full HTML job
    # description on every posting (several MB per company for big boards)
    # — it was dropped earlier specifically to fix an OOM crash, at the cost
    # of losing the `departments` field (also gated behind this flag) and
    # any shot at salary data. Now that responses are parsed with ijson
    # streaming (one job at a time, never the whole response materialized),
    # turning it back on is safe: verified 459KB peak traced memory for
    # Anthropic's 518-job board even with content=true on, vs. ~5.7MB of raw
    # description text streamed through. That gets department back for free
    # and makes best-effort salary extraction possible.
    resp = open_stream(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if resp is None:
        return None
    results = []
    with resp:
        for job in stream_items(resp, "jobs.item"):
            title = job.get("title", "")
            location = job.get("location", {}).get("name", "")
            job_url = job.get("absolute_url", "")
            posted = parse_ts(job.get("updated_at") or job.get("created_at"))
            depts = job.get("departments") or []
            department = depts[0].get("name", "") if depts else ""
            commitment = ""
            for meta in job.get("metadata") or []:
                if (meta.get("name") or "").strip().lower() in ("employment type", "job type", "commitment"):
                    commitment = str(meta.get("value") or "")
                    break
            salary_min, salary_max = extract_salary(strip_html(job.get("content", "")))
            if title and job_url:
                results.append(make_job(title, company, location, posted, job_url, "Greenhouse",
                                         department, commitment, salary_min, salary_max))
    return results


def try_lever(company, slug):
    # Lever has no opt-out for description fields — every posting always
    # includes full description/descriptionPlain/descriptionBody/
    # descriptionBodyPlain HTML+text (measured ~19KB/job average). Streamed
    # so we only ever hold one job's worth of that in memory at a time
    # instead of a whole company's response (which can be several MB). Since
    # these fields are already being pulled down at no extra network/memory
    # cost, salary extraction here is essentially free.
    resp = open_stream(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if resp is None:
        return None
    results = []
    with resp:
        for job in stream_items(resp, "item"):
            if not isinstance(job, dict):
                continue
            title = job.get("text", "")
            cats = job.get("categories", {}) or {}
            locs = cats.get("allLocations") or []
            location = locs[0] if locs else cats.get("location", "")
            job_url = job.get("hostedUrl", "")
            posted = parse_ts(job.get("createdAt"))
            department = cats.get("team", "") or cats.get("department", "")
            commitment = cats.get("commitment", "")
            desc_text = " ".join([
                job.get("descriptionPlain", "") or "",
                job.get("openingPlain", "") or "",
                job.get("additionalPlain", "") or "",
            ])
            salary_min, salary_max = extract_salary(desc_text)
            if title and job_url:
                results.append(make_job(title, company, location, posted, job_url, "Lever",
                                         department, commitment, salary_min, salary_max))
    return results


def salary_from_ashby_compensation(comp):
    """Ashby has a real structured compensation field (min/max/currency per
    tier), gated behind ?includeCompensation=true — not documented anywhere
    obvious, found by comparing the rendered job page (which shows a
    "Compensation" section with per-location pay bands) against what the
    plain API response was missing. When present this is exact data, not a
    regex guess, so it's tried before falling back to description text.
    Spans across all USD Salary components (e.g. separate SF/NY vs.
    rest-of-US tiers) to get the overall min-to-max range, matching what the
    "Compensation" section shows on the actual job page."""
    if not comp:
        return None, None
    lo = hi = None
    for tier in comp.get("compensationTiers") or []:
        for component in tier.get("components") or []:
            if component.get("compensationType") != "Salary":
                continue
            if component.get("currencyCode") != "USD":
                continue  # avoid conflating EUR/GBP/etc. figures with USD
            if component.get("interval") != "1 YEAR":
                continue  # some roles (e.g. contract/IT support) are quoted
                          # hourly ($60.58/hr) — mixing that into an "annual"
                          # range next to $200,000 salaries would be wrong,
                          # not just imprecise
            cmin, cmax = component.get("minValue"), component.get("maxValue")
            # ijson parses JSON numbers with a decimal point as
            # decimal.Decimal (for precision), which sqlite3 can't bind —
            # normalize to plain ints here.
            if cmin is not None:
                cmin = int(round(float(cmin)))
                lo = cmin if lo is None else min(lo, cmin)
            if cmax is not None:
                cmax = int(round(float(cmax)))
                hi = cmax if hi is None else max(hi, cmax)
    return lo, hi


def try_ashby(company, slug):
    # Same story as Lever — descriptionHtml/descriptionPlain are always
    # present on every posting (~17.5KB/job average), no opt-out. Streamed.
    # descriptionPlain is already in memory per-job, so regex salary
    # extraction is free here too, used as a fallback when the structured
    # compensation field (below) isn't present.
    resp = open_stream(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if resp is None:
        return None
    results = []
    with resp:
        for job in stream_items(resp, "jobs.item"):
            title = job.get("title", "")
            location = job.get("location", "") or job.get("locationName", "")
            job_url = job.get("jobUrl", "")
            if not job_url:
                path = job.get("jobPostingPath", "")
                job_url = f"https://jobs.ashbyhq.com/{slug}{path}" if path and not path.startswith("http") else path
            posted = parse_ts(job.get("publishedAt") or job.get("createdAt"))
            department = job.get("department", "") or job.get("team", "")
            commitment = job.get("employmentType", "")
            salary_min, salary_max = salary_from_ashby_compensation(job.get("compensation"))
            if salary_min is None:
                salary_min, salary_max = extract_salary(job.get("descriptionPlain", "") or "")
            if title and job_url:
                results.append(make_job(title, company, location, posted, job_url, "Ashby",
                                         department, commitment, salary_min, salary_max))
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


def scrape_all(companies=None, max_workers=4, progress_cb=None, batch_cb=None, batch_size=100):
    """Scrape every company in COMPANIES (or a provided subset). Thread-pooled
    for speed, but memory-bounded: jobs are buffered only up to `batch_size`
    at a time, then handed to `batch_cb(jobs)` (e.g. a DB upsert) and
    discarded, rather than accumulating ~100k job dicts for the entire
    ~10-minute scrape. If batch_cb is None, falls back to returning
    everything at once (used by tests / small ad-hoc scrapes).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    companies = companies if companies is not None else COMPANIES
    seen_slugs = set()
    clean = []
    for name, slug in companies:
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            clean.append((name, slug))

    buffer = []
    all_jobs = [] if batch_cb is None else None  # only retained without streaming
    total_jobs = 0
    not_found = 0
    found_no_match = 0
    found = 0
    done = 0

    def _flush():
        nonlocal buffer
        if buffer and batch_cb:
            batch_cb(buffer)
        buffer = []
        if batch_cb:
            import gc
            gc.collect()

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
                total_jobs += len(jobs)
                if batch_cb:
                    buffer.extend(jobs)
                    if len(buffer) >= batch_size:
                        _flush()
                else:
                    all_jobs.extend(jobs)
            elif status == "found_no_match":
                found_no_match += 1
            else:
                not_found += 1
            if progress_cb:
                progress_cb(done, len(clean))

    _flush()

    stats = {
        "companies_scanned": len(clean),
        "companies_with_jobs": found,
        "companies_found_no_openings": found_no_match,
        "companies_not_found": not_found,
        "jobs_scraped": total_jobs,
    }
    return (all_jobs or []), stats
