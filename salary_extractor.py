"""
salary_extractor.py — best-effort salary range extraction from unstructured
job description text.

None of Greenhouse, Lever, or Ashby expose salary as a structured field, so
this is a regex-based heuristic, not a guarantee. It's deliberately
conservative: it only matches clearly-formatted dollar ranges (with a $ sign
and comma-thousands or "k" shorthand), so it will miss plenty of real salary
disclosures written in unusual formats, but it should rarely show a *wrong*
number. Values are always surfaced in the UI as "approx." to make clear
they're extracted, not verified.
"""

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# $120,000 - $150,000  /  $120,000-$150,000  /  $120,000 to $150,000
_RANGE_COMMA_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3}){1,2})(?:\.\d{2})?\s*(?:-|–|—|to)\s*\$?\s?(\d{1,3}(?:,\d{3}){1,2})(?:\.\d{2})?",
    re.IGNORECASE,
)

# $120k - $150k  /  $120K-$150K
_RANGE_K_RE = re.compile(
    r"\$\s?(\d{2,3})\s?[kK]\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3})\s?[kK]",
)

# "between $95,000 and $115,000"
_RANGE_BETWEEN_RE = re.compile(
    r"between\s+\$\s?(\d{1,3}(?:,\d{3}){1,2})(?:\.\d{2})?\s+and\s+\$?\s?(\d{1,3}(?:,\d{3}){1,2})(?:\.\d{2})?",
    re.IGNORECASE,
)

# $130,000 per year / $130,000/yr / $130,000 annually (single figure, only
# trusted with an explicit annual-pay suffix to cut down on false positives
# like funding amounts or contract totals)
_SINGLE_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3}){1,2})(?:\.\d{2})?\s*(?:USD)?\s*(?:/\s*(?:yr|year)|per\s+year|annually)",
    re.IGNORECASE,
)

MIN_REASONABLE = 15_000
MAX_REASONABLE = 1_000_000


def strip_html(text):
    """Turn a chunk of job-description HTML into plain text, good enough for
    regex scanning (not for display).

    Some ATS content fields (observed on Greenhouse) come back
    double-escaped — literal `&lt;div&gt;` instead of `<div>`, with the
    original entities like `&mdash;` themselves re-escaped into
    `&amp;mdash;`. Unescaping only once leaves the tags as escaped text,
    which _TAG_RE (already run) never gets a chance to strip, silently
    hiding an "Annual Salary: $X — $Y" block inside literal tag markup.
    Unescaping repeatedly until the string stops changing handles both
    singly- and doubly-escaped input the same way."""
    if not text:
        return ""
    prev = None
    while prev != text:
        prev = text
        text = html.unescape(text)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _valid_pair(lo, hi):
    return MIN_REASONABLE <= lo <= MAX_REASONABLE and MIN_REASONABLE <= hi <= MAX_REASONABLE and lo <= hi


def extract_salary(text):
    """Return (salary_min, salary_max) as ints, or (None, None) if nothing
    confident was found. Scans plain text (call strip_html() first if the
    source was HTML)."""
    if not text:
        return None, None

    m = _RANGE_COMMA_RE.search(text)
    if m:
        lo = int(m.group(1).replace(",", ""))
        hi = int(m.group(2).replace(",", ""))
        if _valid_pair(lo, hi):
            return lo, hi

    m = _RANGE_BETWEEN_RE.search(text)
    if m:
        lo = int(m.group(1).replace(",", ""))
        hi = int(m.group(2).replace(",", ""))
        if _valid_pair(lo, hi):
            return lo, hi

    m = _RANGE_K_RE.search(text)
    if m:
        lo = int(m.group(1)) * 1000
        hi = int(m.group(2)) * 1000
        if _valid_pair(lo, hi):
            return lo, hi

    m = _SINGLE_RE.search(text)
    if m:
        val = int(m.group(1).replace(",", ""))
        if MIN_REASONABLE <= val <= MAX_REASONABLE:
            return val, val

    return None, None
