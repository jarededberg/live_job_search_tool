"""
blurb_extractor.py — best-effort short summary for a role card: prioritizes
an explicit years-of-experience requirement, falls back to the first couple
bullet points under a "Qualifications"/"Requirements"-style heading, falls
back to the first substantive sentence of the description.

Three input shapes, since each ATS structures this differently:
  - "sections": Lever's `lists` field — already split into titled sections
    (e.g. "What We Require" -> "<li>...</li><li>...</li>"), which is far
    more reliable than hunting for a heading in a wall of text. Preferred
    whenever available.
  - "html": Greenhouse's `content` field — one HTML blob with headings and
    bullet lists embedded inline (e.g. <h2><strong>Minimum
    qualifications</strong></h2><ul><li>...).
  - "plain": already-plain text (Ashby's descriptionPlain, or Lever
    postings that had no `lists`) — no tag structure to search, so this
    only gets the years-mention and generic-sentence passes.

Same philosophy as salary_extractor.py: conservative regexes, labeled as
extracted rather than curated, applied to text already in memory during the
scrape (no extra network cost), and capped short.
"""

import re

from salary_extractor import strip_html, unescape_repeated

MAX_BLURB_LEN = 220

# Finds just the core years-of-experience phrase — no surrounding-word
# capture baked into the regex. An earlier version used
# `(?:\S+\s+){0,10}...(?:\s+\S+){0,14}` to grab context in the same regex,
# which measured ~14ms per call on a real ~8KB job description (7.3s for
# 517 jobs) because bounded repetition of a variable-length token class,
# combined with alternation, backtracks heavily on longer text — especially
# when there's no match at all and the engine has to try every position.
# Matching just the anchor phrase here, then grabbing context with plain
# Python string splitting (see _context_window), does the same job in
# actual O(n) time: the same 517-job batch dropped to ~0.02s.
_YEARS_CORE_RE = re.compile(
    r"\d+\+?\s*(?:-|to)\s*\d+\+?\s*years?\b"
    r"|"
    r"\d+\+?\s*years?(?:\s+of)?\s+(?:relevant\s+)?experience\b",
    re.IGNORECASE,
)


def _context_window(text, start, end, before_words=10, after_words=14):
    """Whole preceding/following WORDS (not a fixed character count) around
    text[start:end], so the returned blurb never starts or ends mid-word."""
    before_tokens = text[:start].split()
    after_tokens = text[end:].split()
    before = " ".join(before_tokens[-before_words:]) if before_tokens else ""
    after = " ".join(after_tokens[:after_words]) if after_tokens else ""
    return " ".join(p for p in (before, text[start:end], after) if p)


def _years_match_with_context(text):
    m = _YEARS_CORE_RE.search(text)
    if not m:
        return None
    return _context_window(text, m.start(), m.end())

# Matched against real (unescaped) HTML tag structure, not tag-stripped
# text — checking stripped text picked up stray uses of "requirements" or
# "qualifications" mid-sentence (e.g. "compliance requirements") that had
# nothing to do with an actual qualifications list.
_HEADING_TAG_RE = re.compile(
    r"<(h[1-6]|strong|b)[^>]*>\s*(?:minimum\s+|required\s+|preferred\s+|basic\s+)?"
    r"(?:qualifications|requirements|"
    r"what you'?ll (?:bring|need)|who you are|what we'?re looking for)\s*</\1>",
    re.IGNORECASE,
)
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)

_QUAL_KEYWORDS = (
    "require", "qualif", "what you'll need", "what you'll bring",
    "who you are", "what we're looking for", "minimum qualifications", "basic qualifications",
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WS_RE = re.compile(r"\s+")


def _clean(s):
    return _WS_RE.sub(" ", s).strip()


def _truncate(s, limit=MAX_BLURB_LEN):
    s = s.strip()
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:.-") + "…"


def _bullets_from_li_blob(html_fragment):
    items = _LI_RE.findall(html_fragment or "")
    cleaned = [strip_html(it) for it in items[:3]]
    cleaned = [c for c in cleaned if len(c) >= 8]
    return "; ".join(cleaned) if cleaned else None


def _bullets_after_heading_in_blob(html):
    m = _HEADING_TAG_RE.search(html)
    if not m:
        return None
    window = html[m.end(): m.end() + 1500]  # don't scan the whole doc for stray <li>s
    return _bullets_from_li_blob(window)


def _is_quals_heading(title):
    t = (title or "").lower()
    return any(k in t for k in _QUAL_KEYWORDS)


def extract_blurb_from_sections(sections):
    """sections: list of (title, html_content) tuples, e.g. Lever's `lists`
    field. Checks each section's plain text for a years-mention first (most
    reliable signal regardless of which section it's in), then falls back
    to bullets from the first section whose title looks like a
    qualifications/requirements heading."""
    if not sections:
        return ""

    for _title, content in sections:
        plain = strip_html(content or "")
        ctx = _years_match_with_context(plain)
        if ctx:
            return _truncate(_clean(ctx))

    for title, content in sections:
        if _is_quals_heading(title):
            bullets = _bullets_from_li_blob(unescape_repeated(content or ""))
            if bullets:
                return _truncate(_clean(bullets))

    # Nothing quals-shaped; just describe the role from the first section
    # with real content (skip section headings that are just the job title
    # repeated back, which Lever includes as the first list item)
    for _title, content in sections:
        plain = strip_html(content or "")
        if len(plain) >= 60:
            sentences = _SENTENCE_SPLIT_RE.split(plain)
            for s in sentences:
                if len(s) >= 40:
                    return _truncate(s)
    return ""


def extract_blurb(html_or_text, is_html=True):
    """Return a short (<= MAX_BLURB_LEN char) plain-text blurb, or "" if
    there's nothing worth showing. `html_or_text` is the RAW source (HTML
    by default) — pass is_html=False for sources that are already plain
    text, which skips the HTML-only bullet-list step since there are no
    tags left to find headings in. For Lever's structured `lists` field,
    use extract_blurb_from_sections() instead."""
    if not html_or_text or len(html_or_text) < 40:
        return ""

    plain = strip_html(html_or_text) if is_html else html_or_text

    ctx = _years_match_with_context(plain)
    if ctx:
        return _truncate(_clean(ctx))

    if is_html:
        # unescape (possibly repeatedly) first — some sources double-escape
        # their HTML, so the tags _HEADING_TAG_RE looks for may only exist
        # as literal "&lt;h2&gt;" text otherwise, never matching anything.
        bullets = _bullets_after_heading_in_blob(unescape_repeated(html_or_text))
        if bullets:
            return _truncate(_clean(bullets))

    sentences = _SENTENCE_SPLIT_RE.split(_clean(plain))
    for s in sentences:
        if len(s) >= 60:
            return _truncate(s)

    return _truncate(_clean(plain)) if plain.strip() else ""
