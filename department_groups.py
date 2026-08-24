"""
department_groups.py — canonical department categories that collapse the
long tail of raw scraped "department" strings into the small, familiar set
of buckets most job boards use (Engineering, Sales, Marketing, etc).

Real motivation: raw ATS department values are all over the place --
"Engineering", "Software Engineering", "Engineering - Pipeline", "Field
Engineering - Other", "AI Research & Engineering", "EPD", "GTM",
"Go-To-Market", "20213 S&M - Sales - Square Outside", "HQ Management" --
because Greenhouse/Lever/Ashby just store whatever free-text label (or
internal org-chart code) the company typed into their ATS. A department
picker built straight off `SELECT DISTINCT department` is unusable -- a
user reported it as "weird" and unlike the clean department dropdowns on
LinkedIn/Indeed. This module classifies each raw string into one of a
fixed set of canonical labels via keyword matching, same approach as
location_groups.py uses for location strings.

Like location_groups.py, this is a classifier over the raw string, not an
exact-match lookup table -- there's no way to enumerate every company's
spelling in advance. Order matters: DEPARTMENT_GROUPS is checked
top-to-bottom and the first matching group wins, so more specific/
technical-but-customer-facing labels (e.g. "Solutions Engineering", which
reads as Sales on every major job board despite containing "engineering")
are listed ahead of the broad "Engineering" bucket that would otherwise
swallow them.
"""

import re

_HYPHEN_AMP_RE = re.compile(r"[-&/]")
_WS_RE = re.compile(r"\s+")


def _normalize(raw):
    """Lowercase, and turn "-", "&", "/" into spaces so phrase keywords
    match regardless of whether the raw string uses "Go-To-Market",
    "Go To Market", or "Go/To/Market" -- real scraped values use all of
    these inconsistently for the same thing."""
    s = _HYPHEN_AMP_RE.sub(" ", raw.lower())
    return _WS_RE.sub(" ", s).strip()


def _kw(*phrases):
    """Compile a list of keyword phrases into one regex: multi-word
    phrases match as plain substrings, single short tokens (<=3 chars,
    e.g. "it", "hr", "pr", "ux", "ui", "qa") get \\b word boundaries so
    they don't false-positive inside unrelated words."""
    parts = []
    for p in phrases:
        if " " in p or len(p) > 3:
            parts.append(re.escape(p))
        else:
            parts.append(r"\b" + re.escape(p) + r"\b")
    return re.compile("|".join(parts))


# Ordered top-to-bottom; first match wins. Each entry is
# (canonical_label, compiled_keyword_regex).
DEPARTMENT_GROUPS_ORDER = [
    ("Sales", _kw(
        "sales", "revenue", "account executive", "business development",
        "partnerships", "gtm", "go to market", "enterprise sales",
        "sales development", "solution engineering", "solutions engineering",
        "customer engineering", "sales engineering",
    )),
    # Checked before Engineering: "Information Technology" contains
    # "technology", which would otherwise match Engineering's broader
    # "technology" keyword first -- IT (corporate/internal IT, helpdesk)
    # and Engineering (product/software engineering) are different
    # functions even though the raw strings overlap textually.
    ("IT", _kw("information technology", "helpdesk", "help desk", "it")),
    ("Engineering", _kw(
        "engineering", "engineer", "software", "developer", "dev",
        "technology", "applied ai", "ai research", "research and development",
        "research", "r&d", "data center", "epd", "infrastructure", "platform",
        "security", "devops", "qa", "quality assurance", "site reliability",
    )),
    ("Product", _kw("product management", "product")),
    ("Design", _kw("design", "ux", "ui", "user experience")),
    ("Marketing", _kw("marketing", "growth", "brand", "communications", "content")),
    ("Customer Success", _kw(
        "customer success", "customer support", "customer experience",
        "customer service", "client services", "support",
    )),
    ("Professional Services", _kw("professional services", "consulting", "implementation", "delivery")),
    ("Operations", _kw(
        "operations", "business operations", "hq management", "supply chain",
        "logistics", "manufacturing", "facilities", "scaling", "user operations",
    )),
    ("Finance", _kw("finance", "accounting", "fp&a", "financial")),
    ("People", _kw("people", "human resources", "hr", "talent", "recruiting", "recruitment")),
    ("Legal", _kw("legal", "compliance", "policy")),
    ("Data", _kw("data science", "data analytics", "analytics", "business intelligence", "data")),
    ("Executive", _kw("executive", "leadership", "chief of staff", "office of the ceo")),
]

# Fixed display order for the picker -- roughly "most people are looking
# for this" first, rather than raw frequency, so the list reads like a
# normal job board's department filter instead of shuffling every time the
# underlying data changes. Deliberately NOT the same order as
# DEPARTMENT_GROUPS_ORDER above -- that order is tuned for classification
# priority (e.g. IT has to be checked before Engineering to disambiguate
# "Information Technology"), which isn't the order a person would expect
# to see these listed in. distinct_facet_values()/department_group_facets()
# in db.py still only include a label here if at least one job currently
# classifies into it, and appends "Other" last when applicable.
DEPARTMENT_DISPLAY_ORDER = [
    "Engineering", "Product", "Design", "Sales", "Marketing",
    "Customer Success", "Operations", "Data", "IT", "Finance", "People",
    "Legal", "Professional Services", "Executive",
]

assert set(DEPARTMENT_DISPLAY_ORDER) == {label for label, _ in DEPARTMENT_GROUPS_ORDER}, \
    "DEPARTMENT_DISPLAY_ORDER must list exactly the same labels as DEPARTMENT_GROUPS_ORDER"


def classify_department(raw):
    """Canonical label for a raw scraped department string, or None if
    nothing matches (caller decides whether that becomes an "Other"
    bucket or gets dropped)."""
    if not raw:
        return None
    norm = _normalize(raw)
    for label, pattern in DEPARTMENT_GROUPS_ORDER:
        if pattern.search(norm):
            return label
    return None


DEPARTMENT_LABELS = set(DEPARTMENT_DISPLAY_ORDER)
