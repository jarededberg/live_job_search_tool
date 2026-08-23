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

# "$179,000/yr to $220,000/yr" -- each number carries its own "/yr" suffix
# instead of one shared suffix at the end, which _RANGE_COMMA_RE's plain
# "<num> - <num>" separator doesn't expect (the "/yr" in between breaks
# it). Real postings use this a lot when they're generated from a
# structured pay-band field rather than hand-typed prose.
_RANGE_YR_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3}){1,2})(?:\.\d{2})?\s*(?:/\s*(?:yr|year)|per\s+year)"
    r"\s*(?:-|–|—|to)\s*"
    r"\$?\s?(\d{1,3}(?:,\d{3}){1,2})(?:\.\d{2})?\s*(?:/\s*(?:yr|year)|per\s+year)?",
    re.IGNORECASE,
)

# $130,000 per year / $130,000/yr / $130,000 annually (single figure, only
# trusted with an explicit annual-pay suffix to cut down on false positives
# like funding amounts or contract totals)
_SINGLE_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3}){1,2})(?:\.\d{2})?\s*(?:USD)?\s*(?:/\s*(?:yr|year)|per\s+year|annually)",
    re.IGNORECASE,
)

# Tried in this order; each pattern is scanned for ALL its matches (not
# just the first) before moving to the next pattern -- see extract_salary.
_RANGE_PATTERNS = [_RANGE_COMMA_RE, _RANGE_YR_RE, _RANGE_BETWEEN_RE, _RANGE_K_RE]

MIN_REASONABLE = 15_000
MAX_REASONABLE = 1_000_000


def unescape_repeated(text):
    """Some ATS content fields (observed on Greenhouse) come back
    double-escaped — literal `&lt;div&gt;` instead of `<div>`, with the
    original entities like `&mdash;` themselves re-escaped into
    `&amp;mdash;`. Unescaping only once leaves the tags as escaped text
    instead of real markup. Unescaping repeatedly until the string stops
    changing handles both singly- and doubly-escaped input the same way,
    and yields real `<tag>` markup for anything that still wants to look
    at tag structure (see blurb_extractor.py) rather than just plain text."""
    if not text:
        return ""
    prev = None
    while prev != text:
        prev = text
        text = html.unescape(text)
    return text


def strip_html(text):
    """Turn a chunk of job-description HTML into plain text, good enough for
    regex scanning (not for display)."""
    text = unescape_repeated(text)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _valid_pair(lo, hi):
    return MIN_REASONABLE <= lo <= MAX_REASONABLE and MIN_REASONABLE <= hi <= MAX_REASONABLE and lo <= hi


def extract_salary(text):
    """Return (salary_min, salary_max) as ints, or (None, None) if nothing
    confident was found. Scans plain text (call strip_html() first if the
    source was HTML).

    For each range pattern, checks EVERY match in the text (via
    `finditer`), not just the first one, and returns the first match that
    passes `_valid_pair` -- not necessarily the first match found at all.
    This matters for real postings with more than one dollar figure before
    a valid comp range: multi-location postings often list several pay
    bands ("$X-$Y in Denver... $A-$B in San Francisco"), and typo'd
    figures do happen (`_valid_pair` rejecting a clearly out-of-range
    number like a stray extra ",000" used to mean the whole pattern gave
    up instead of continuing on to the next, genuinely valid, match later
    in the same text).
    """
    if not text:
        return None, None

    for pattern in _RANGE_PATTERNS:
        is_k = pattern is _RANGE_K_RE
        for m in pattern.finditer(text):
            lo = int(m.group(1).replace(",", ""))
            hi = int(m.group(2).replace(",", ""))
            if is_k:
                lo *= 1000
                hi *= 1000
            if _valid_pair(lo, hi):
                return lo, hi

    for m in _SINGLE_RE.finditer(text):
        val = int(m.group(1).replace(",", ""))
        if MIN_REASONABLE <= val <= MAX_REASONABLE:
            return val, val

    return None, None
