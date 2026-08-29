"""
role_groups.py — canonical role-title categories that collapse the long
tail of raw scraped job titles into a small, fixed set of familiar role
families (Software Engineer, Product Manager, Account Executive, ...),
purely for the salary insight pages (see app.py's /salary/<slug> routes
and db.py's salary_stats_by_role()).

Same reasoning and approach as department_groups.py: raw job titles are
free text pulled straight off each company's ATS ("Sr. Backend Engineer
II", "Founding Software Engineer, Platform", "Software Engineer (Remote)")
and there's no way to enumerate every real-world spelling in advance, so
this classifies each title into a canonical bucket via ordered keyword
matching (first match wins) rather than an exact-match lookup.

This is a DIFFERENT module from role_synonyms.py on purpose, not a
duplicate: role_synonyms.py expands a resume's extracted phrase into a
list of related terms for keyword *search* (many-to-many, no fixed
label set, tuned for recall). This module instead classifies a title
into exactly one canonical bucket for *aggregation* — every job needs to
land in a single, stable bucket for the salary-by-role rollup to make
sense, the same way department_groups.py needs one canonical department
per job rather than a list of related departments.

Deliberately curated and non-exhaustive, same as department_groups.py —
covers common tech/business role families most job seekers would
actually search a "software engineer salary" / "product manager salary"
style query for. A title that matches nothing returns None; callers
simply don't include that job in any role-level rollup (it still shows
up in the per-company rollup and everywhere else on the site, just not
under a role page).
"""

import re

_HYPHEN_AMP_RE = re.compile(r"[-&/]")
_WS_RE = re.compile(r"\s+")


def _normalize(raw):
    s = _HYPHEN_AMP_RE.sub(" ", raw.lower())
    return _WS_RE.sub(" ", s).strip()


def _kw(*phrases):
    """Same convention as department_groups.py's _kw(): multi-word
    phrases match as plain substrings, single short tokens (<=3 chars)
    get \\b word boundaries so they don't false-positive inside an
    unrelated word (e.g. bare "ea" or "it")."""
    parts = []
    for p in phrases:
        if " " in p or len(p) > 3:
            parts.append(re.escape(p))
        else:
            parts.append(r"\b" + re.escape(p) + r"\b")
    return re.compile("|".join(parts))


# Ordered top-to-bottom; first match wins. Each entry is
# (canonical_label, slug, compiled_keyword_regex). More specific/narrow
# buckets are listed ahead of broader catch-alls that would otherwise
# swallow them -- e.g. "Product Marketing Manager" is checked before the
# generic "Marketing Manager" bucket (whose own "marketing manager"
# keyword would otherwise match "Product Marketing Manager" too), and
# "Sales Engineer" / "Solutions Engineer" is checked before the generic
# "Software Engineer" bucket's broad "engineer" keyword.
ROLE_GROUPS_ORDER = [
    ("Product Marketing Manager", "product-marketing-manager", _kw(
        "product marketing", "pmm",
    )),
    ("Sales Engineer", "sales-engineer", _kw(
        "solutions engineer", "sales engineer", "pre sales", "presales",
        "solutions engineering", "sales engineering",
    )),
    ("Data Scientist", "data-scientist", _kw(
        "data scientist", "machine learning engineer", "ml engineer",
        "applied scientist", "research scientist", "data science",
    )),
    ("Data Analyst", "data-analyst", _kw(
        "data analyst", "business analyst", "business intelligence",
        "data analytics", "analytics engineer",
    )),
    ("Financial Analyst", "financial-analyst", _kw(
        "financial analyst", "fp&a", "fp a", "finance manager", "financial planning",
    )),
    ("Program Manager", "program-manager", _kw(
        "program manager", "project manager", "technical program manager",
        "tpm", "pmo", "project management", "program management",
    )),
    ("Product Manager", "product-manager", _kw(
        "product manager", "product owner", "product management",
        "technical product manager",
    )),
    ("Product Designer", "product-designer", _kw(
        "product designer", "ux designer", "ui designer",
        "user experience designer", "ux ui designer",
    )),
    ("Account Executive", "account-executive", _kw(
        "account executive", "business development representative",
        "sales development representative", "sales development", "sdr", "bdr",
        "sales representative", "outside sales", "inside sales",
        "enterprise sales", "business development",
    )),
    ("Customer Success Manager", "customer-success-manager", _kw(
        "customer success", "csm", "client success", "customer experience",
        "account management",
    )),
    ("Recruiter", "recruiter", _kw(
        "recruiter", "talent acquisition", "technical recruiter", "sourcer",
        "recruiting",
    )),
    ("HR Business Partner", "hr-business-partner", _kw(
        "human resources", "people operations", "hr business partner",
        "hrbp", "people ops", "hr generalist", "people partner",
    )),
    ("Marketing Manager", "marketing-manager", _kw(
        "marketing manager", "growth marketing", "demand generation",
        "digital marketing", "performance marketing", "marketing",
    )),
    ("Chief of Staff", "chief-of-staff", _kw(
        "chief of staff", "corporate strategy", "strategic planning",
        "strategy consultant", "strategy operations", "strategy and operations",
    )),
    ("Supply Chain Manager", "supply-chain-manager", _kw(
        "supply chain", "logistics", "procurement", "sourcing manager",
    )),
    ("Operations Manager", "operations-manager", _kw(
        "operations manager", "ops manager", "business operations",
        "revenue operations", "sales operations", "biz ops", "bizops",
        "gtm operations", "commercial operations", "operations analyst",
    )),
    ("Accountant", "accountant", _kw(
        "accountant", "controller", "bookkeeper", "staff accountant", "accounting",
    )),
    ("IT Support", "it-support", _kw(
        "it support", "systems administrator", "sysadmin",
        "network administrator", "helpdesk", "help desk", "it manager",
    )),
    ("Legal Counsel", "legal-counsel", _kw(
        "legal counsel", "attorney", "paralegal", "compliance",
        "corporate counsel",
    )),
    ("Executive Assistant", "executive-assistant", _kw(
        "executive assistant", "administrative assistant", "office manager",
    )),
    ("Consultant", "consultant", _kw(
        "management consulting", "management consultant", "consulting", "consultant",
    )),
    ("Software Engineer", "software-engineer", _kw(
        "software engineer", "backend engineer", "frontend engineer",
        "full stack", "fullstack", "mobile engineer", "site reliability engineer",
        "devops engineer", "platform engineer", "infrastructure engineer",
        "software developer", "swe",
    )),
]

# Display order for the /salary index page -- roughly "most-searched
# first" rather than the classification-precedence order above (which is
# tuned for disambiguation, not what a person would expect to browse in
# first, same distinction department_groups.py draws between
# DEPARTMENT_GROUPS_ORDER and DEPARTMENT_DISPLAY_ORDER).
ROLE_DISPLAY_ORDER = [
    "Software Engineer", "Product Manager", "Data Scientist", "Data Analyst",
    "Product Designer", "Program Manager", "Account Executive", "Sales Engineer",
    "Customer Success Manager", "Marketing Manager", "Product Marketing Manager",
    "Recruiter", "HR Business Partner", "Financial Analyst", "Accountant",
    "Operations Manager", "Chief of Staff", "Supply Chain Manager", "IT Support",
    "Legal Counsel", "Executive Assistant", "Consultant",
]

assert set(ROLE_DISPLAY_ORDER) == {label for label, _slug, _re in ROLE_GROUPS_ORDER}, \
    "ROLE_DISPLAY_ORDER must list exactly the same labels as ROLE_GROUPS_ORDER"

ROLE_SLUGS = {label: slug for label, slug, _re in ROLE_GROUPS_ORDER}
ROLE_LABELS_BY_SLUG = {slug: label for label, slug, _re in ROLE_GROUPS_ORDER}


def classify_role(title):
    """Canonical role label for a raw scraped job title, or None if
    nothing matches. Title-only (not blurb/department) -- the role
    families here are specifically about what the JOB IS CALLED, since
    that's the actual "software engineer salary" style query a visitor
    lands on this page from."""
    if not title:
        return None
    norm = _normalize(title)
    for label, _slug, pattern in ROLE_GROUPS_ORDER:
        if pattern.search(norm):
            return label
    return None
