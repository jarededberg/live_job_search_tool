"""
location_groups.py — canonical "Remote (US)" / "Remote (Canada)" / etc.
filters that collapse dozens of raw scraped location-string variants into
one selectable option.

Real motivation: a single 800-company sample turned up 865 distinct
location strings containing the word "remote" — "Remote - US", "US
Remote", "REMOTE - USA", "California, USA, Remote", "Remote-US-CA",
"AMER-US-Remote", and on and on, because Greenhouse/Lever/Ashby just store
whatever free-text string (or semicolon-joined list of offices +  remote
options) the company typed into their ATS. Picking "Remote (US)" here is
meant to mean "any of those", not "the one exact string spelled that way".

These are regex/keyword classifiers over the RAW location string, not an
exact-match lookup table — there's no way to enumerate every real-world
spelling in advance, so a job matches a group if its location string
contains a recognizable signal for that group (a state/province name, a
country name/abbreviation, etc.) alongside the word "remote".
"""

import re

_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
}
# Case-sensitive whole-word check (these are ordinary English words when
# lowercased — "or", "in", "me", "hi" — so only matched against the raw,
# original-case string, which is safe here because location strings are
# short structured fields ("Atlanta, GA"), not free-form prose).
US_STATE_ABBR_RE = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
    r"MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|"
    r"UT|VT|VA|WA|WV|WI|WY|DC)\b"
)
US_WORD_RE = re.compile(r"\b(us|usa|u\.s\.|u\.s\.a\.|united states)\b", re.IGNORECASE)

CANADA_PROVINCE_NAMES = {
    "alberta", "british columbia", "ontario", "quebec", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland",
    "prince edward island", "yukon", "northwest territories", "nunavut",
}
CANADA_PROVINCE_ABBR_RE = re.compile(r"\b(AB|BC|ON|QC|MB|SK|NS|NB|NL|PE|YT|NT|NU)\b")
CANADA_WORD_RE = re.compile(r"\bcanada\b", re.IGNORECASE)

UK_WORD_RE = re.compile(
    r"\b(uk|u\.k\.|united kingdom|england|scotland|wales|northern ireland)\b",
    re.IGNORECASE,
)

EUROPE_COUNTRY_NAMES = {
    "germany", "france", "spain", "italy", "netherlands", "belgium",
    "sweden", "norway", "denmark", "finland", "poland", "portugal",
    "austria", "switzerland", "ireland", "greece", "czech republic",
    "hungary", "romania", "europe", "emea",
}


def _mentions_us(loc):
    if US_WORD_RE.search(loc):
        return True
    low = loc.lower()
    if any(name in low for name in US_STATE_NAMES):
        return True
    return bool(US_STATE_ABBR_RE.search(loc))


def _mentions_canada(loc):
    if CANADA_WORD_RE.search(loc):
        return True
    low = loc.lower()
    if any(name in low for name in CANADA_PROVINCE_NAMES):
        return True
    return bool(CANADA_PROVINCE_ABBR_RE.search(loc))


def _mentions_uk(loc):
    return bool(UK_WORD_RE.search(loc))


def _mentions_europe(loc):
    low = loc.lower()
    return any(name in low for name in EUROPE_COUNTRY_NAMES) or _mentions_uk(loc)


def _is_remote(loc):
    return bool(_REMOTE_RE.search(loc))


LOCATION_GROUPS = {
    "remote_us": {
        "label": "Remote (US)",
        "match": lambda loc: _is_remote(loc) and _mentions_us(loc),
    },
    "remote_canada": {
        "label": "Remote (Canada)",
        "match": lambda loc: _is_remote(loc) and _mentions_canada(loc),
    },
    "remote_uk": {
        "label": "Remote (UK)",
        "match": lambda loc: _is_remote(loc) and _mentions_uk(loc),
    },
    "remote_europe": {
        "label": "Remote (Europe)",
        "match": lambda loc: _is_remote(loc) and _mentions_europe(loc) and not _mentions_uk(loc),
    },
    "remote_anywhere": {
        "label": "Remote (unspecified / global)",
        "match": lambda loc: (
            _is_remote(loc)
            and not _mentions_us(loc)
            and not _mentions_canada(loc)
            and not _mentions_europe(loc)
        ),
    },
}


def matches_group(group_key, location):
    """True if `location` (a raw scraped location string) belongs to the
    named canonical group. Unknown group keys return False rather than
    raising, since the key only ever comes from a query param."""
    group = LOCATION_GROUPS.get(group_key)
    if not group or not location:
        return False
    return group["match"](location)
