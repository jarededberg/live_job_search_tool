"""
build_logo_cache.py — one-time (well, occasional) precompute script that
figures out, for every company in companies_data.py, a domain whose favicon
Google's favicon service can actually serve, and writes the result to
logo_cache.json as {company_name: domain_or_null}.

Why precompute instead of guessing live per page render: Clearbit's logo API
(what this originally used) is dead, and Google's favicon service
(https://www.google.com/s2/favicons?domain=X) doesn't know a company's real
domain from its ATS board slug — "scaleai" (the Greenhouse slug) 404s,
"scale.com" (the real domain) works. So each company needs 1-8 quick HEAD
requests to find a domain that actually resolves to a favicon, and there's
no reason to repeat that lookup on every page load for every visitor when
there are only ~4,300 companies and their domains basically never change.
Run this occasionally (e.g. whenever companies_data.py gets new entries),
not on every deploy.

Usage:
    python3 build_logo_cache.py [--limit N] [--start-index N] [--workers N]

Writes/updates logo_cache.json incrementally (safe to Ctrl-C and resume,
and safe to re-run — already-cached companies are skipped).
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

from companies_data import COMPANIES

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Written straight into static/ since that's what app.py actually serves
# (static_url_path="" means /logo_cache.json resolves there) — no separate
# copy step to forget.
CACHE_PATH = os.path.join(APP_DIR, "static", "logo_cache.json")
TLDS = (".com", ".ai", ".io", ".co")


def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Some company names already spell out their real domain, e.g. "Apollo.io"
# or "Character.AI" — stripping the dot and appending ".com" (the generic
# guess) produces a *different domain that happens to resolve* (apolloio.com
# is a live but unrelated site), which is worse than useless: it's a
# confident-looking wrong logo, not a missing one. Whatever TLD is already
# embedded in the name is a much stronger signal than any guess, so it's
# tried first.
_NAME_TLD_RE = re.compile(r"^([A-Za-z0-9]+)\.(io|ai|co|com|so|dev|app|xyz)\b", re.IGNORECASE)


def candidates_for(name, slug):
    cands = []

    m = _NAME_TLD_RE.match(name.strip())
    if m:
        cands.append(f"{m.group(1).lower()}.{m.group(2).lower()}")

    # Strip anything after a "/" or "(" — a lot of entries here are written
    # as "Cursor / Anysphere" or "Foo (formerly Bar)"; only the first chunk
    # is worth trying as a domain base.
    name_base = normalize(re.split(r"[/(]", name)[0])
    slug_base = normalize(slug)
    bases = [b for b in dict.fromkeys([slug_base, name_base]) if b]
    for base in bases:
        for tld in TLDS:
            cands.append(base + tld)
    return list(dict.fromkeys(cands))


def check_domain(domain, timeout=6):
    url = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except HTTPError:
        return False
    except URLError:
        return False
    except Exception:
        return False


def resolve_company(name, slug):
    for domain in candidates_for(name, slug):
        if check_domain(domain):
            return domain
    return None


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
    os.replace(tmp, CACHE_PATH)


def main():
    limit = None
    start_index = 0
    workers = 24
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--limit":
            limit = int(args[i + 1])
        if a == "--start-index":
            start_index = int(args[i + 1])
        if a == "--workers":
            workers = int(args[i + 1])

    # de-dupe companies_data.py the same way scraper.py does
    seen = set()
    all_companies = []
    for name, slug in COMPANIES:
        if slug not in seen:
            seen.add(slug)
            all_companies.append((name, slug))

    cache = load_cache()
    todo = [(n, s) for n, s in all_companies[start_index:] if n not in cache]
    if limit:
        todo = todo[:limit]

    print(f"total companies: {len(all_companies)}, already cached: {len(cache)}, resolving now: {len(todo)}")

    t0 = time.time()
    found = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(resolve_company, name, slug): name for name, slug in todo}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                domain = fut.result()
            except Exception:
                domain = None
            cache[name] = domain
            if domain:
                found += 1
            done += 1
            if done % 100 == 0:
                save_cache(cache)
                elapsed = time.time() - t0
                print(f"  {done}/{len(todo)} done, {found} resolved, {elapsed:.0f}s elapsed")

    save_cache(cache)
    elapsed = time.time() - t0
    print(f"done: {done} processed, {found} resolved ({found/max(done,1)*100:.0f}%), {elapsed:.0f}s")


if __name__ == "__main__":
    main()
