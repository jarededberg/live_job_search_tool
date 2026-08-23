"""
blurb_extractor.py — best-effort short summary for a role card, styled after
what a real "great job posting" site (hiring.cafe was the specific
reference) shows: a compact years-of-experience badge PLUS a specific
sentence pulled from actual qualifications/responsibilities content — never
a generic "About [Company]" paragraph, which is identical across every
posting from that company and says nothing about the specific role.

Two things get extracted, not one:
  - `years_experience` (via extract_years_experience): just the bare phrase
    ("5+", "3-5"), meant to render as its own small badge.
  - blurb text (via extract_blurb / extract_blurb_from_sections): the
    qualifications bullets under a heading, PRIORITIZED over everything
    else, since real bullet content is far more specific/useful than a
    single sentence built around wherever "years" happens to appear. Only
    falls back to the years-context sentence, then a generic sentence, if
    no bullets can be found at all — and the generic-sentence fallback
    actively skips past any "About [Company]" / "About Us" intro block
    instead of grabbing it (see _skip_about_block), since that block is
    boilerplate, not a description of this specific job.

Three input shapes, since each ATS structures this differently:
  - "sections": Lever's `lists` field — already split into titled sections
    (e.g. "What We Require" -> "<li>...</li><li>...</li>"), which is far
    more reliable than hunting for a heading in a wall of text. Preferred
    whenever available.
  - "html": Greenhouse's `content` field — one HTML blob with headings and
    bullet lists embedded inline (e.g. <h2><strong>Minimum
    qualifications</strong></h2><ul><li>...). Real postings routinely put
    "About [Company]" as the literal first paragraph, before any
    qualifications heading — that's exactly the trap the old fallback fell
    into.
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
    r"(\d+\+?)\s*(?:-|to)\s*(\d+\+?)\s*years?\b"
    r"|"
    r"(\d+\+?)\s*years?(?:\s+of)?\s+(?:relevant\s+)?experience\b",
    re.IGNORECASE,
)


def extract_years_experience(text):
    """Returns a short badge string ("5+", "3-5") or None. Searches the
    given text (should be plain, tag-stripped text) for the first
    years-of-experience mention and returns just the number part, not the
    surrounding sentence — that's a separate UI element now (a badge), not
    baked into the blurb text."""
    if not text:
        return None
    m = _YEARS_CORE_RE.search(text)
    if not m:
        return None
    if m.group(1) and m.group(2):
        return f"{m.group(1)}-{m.group(2)}"
    return m.group(3)


_YEARS_RANGE_PARSE_RE = re.compile(r"^(\d+)\+?(?:\s*-\s*(\d+)\+?)?$")


def parse_years_range(value):
    """Parses a years_experience badge string (as produced by
    extract_years_experience above -- "5", "5+", or "3-5") into a
    (lo, hi) int tuple for numeric range-filter comparisons (see db.py's
    salary/YOE range slider filtering).

    "3-5" -> (3, 5). "5+" -> (5, None) -- open-ended, no known upper
    bound, since the posting only said "5+ years," not an actual ceiling.
    A bare "5" -> (5, 5). Missing/unparseable input -> (None, None).
    """
    if not value:
        return None, None
    m = _YEARS_RANGE_PARSE_RE.match(value.strip())
    if not m:
        return None, None
    lo = int(m.group(1))
    if m.group(2):
        return lo, int(m.group(2))
    if value.strip().endswith("+"):
        return lo, None
    return lo, lo


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
#
# Broadened from the original version, which only matched an EXACT "minimum/
# required/preferred/basic + qualifications/requirements/..." phrase with
# nothing else in the tag — real postings routinely write "Desired
# Qualifications:" (colon + a prefix word that wasn't in the allowed list at
# all), "Key Responsibilities", "Essential Duties", "Must Haves", "What
# You'll Bring", etc. A single real Greenhouse posting (Redwood Materials,
# "Abuse Test Engineer, Energy Storage") is what caught this: its actual
# heading was "Desired Qualifications:" and the old regex never matched it,
# so the blurb fell all the way through to the "About Redwood Materials"
# boilerplate paragraph instead.
_HEADING_TAG_RE = re.compile(
    r"<(h[1-6]|strong|b)[^>]*>\s*"
    r"(?:minimum|required|preferred|basic|desired|additional|key|core|essential|general)?\s*"
    r"(?:qualifications|requirements|responsibilities|duties|"
    r"must[\s-]?haves?|"
    r"(?:what|things?)\s+you'?ll?\s+(?:bring|need|do|have|love)|what you (?:bring|have)|"
    r"you should have|you'?ll (?:bring|need|have)|you have|"
    r"who you are|what we'?re looking for|skills? (?:needed|required))"
    r"\s*:?\s*</\1>",
    re.IGNORECASE,
)
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)

_QUAL_KEYWORDS = (
    "require", "qualif", "responsibilit", "duties", "must have", "must-have",
    "what you'll need", "what you'll bring", "what you bring",
    "who you are", "what we're looking for", "skills needed", "skills required",
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


# Matches a "About [Company]" / "About Us" / "Company Overview" / "Who We
# Are" / "Our Mission" style intro heading immediately followed by its
# boilerplate paragraph. Deliberately NOT reliant on newlines/line-anchors:
# strip_html() (from salary_extractor.py) already collapses all whitespace,
# including the original paragraph breaks, into single spaces before any of
# this ever runs — so a real posting's "About Redwood Materials" heading
# followed by "Redwood is localizing a global battery supply chain..."
# arrives here as one continuous run of text with no punctuation between
# the heading and the paragraph at all. Matching the heading phrase at the
# very start of the text, stopping right before the next capitalized word,
# is what actually finds the heading/paragraph boundary in flattened text
# like this.
_COMPANY_INTRO_HEADING_RE = re.compile(
    r"^(?:about\s+(?:us|we|the\s+company|[a-z0-9][\w .,&'\-]{0,50}?)|"
    r"company\s+overview|our\s+company|our\s+mission|who\s+we\s+are|overview)"
    r"\s*:?\s+(?=[A-Z])",
    re.IGNORECASE,
)

# A company-intro block is often several sentences of marketing copy, not
# just one (mission statement, then "we offer...", then culture/values) —
# a real example (Digible's "Company Overview:") ran 3+ sentences before
# reaching anything job-specific. Rather than guessing a fixed sentence
# count, keep dropping sentences that still read like generic company
# marketing language, up to a small cap so an unusually long intro can't
# eat the whole posting.
_COMPANY_MARKETING_SIGNAL_RE = re.compile(
    r"\b(founded|headquartered|is a leading|is a global|our mission|we believe|"
    r"we offer|we pride ourselves|our culture|our values|our people|pioneering|"
    r"revolutioniz|is transforming|is redefining|privately owned|diverse group|"
    r"core values|love to celebrate)\b",
    re.IGNORECASE,
)
_MAX_INTRO_SENTENCES_SKIPPED = 4


def _skip_about_block(text):
    """If `text` (already whitespace-flattened) opens with a company-intro
    heading, drop that heading phrase, then keep dropping sentences that
    still read like generic company marketing copy (capped at
    `_MAX_INTRO_SENTENCES_SKIPPED`), returning whatever comes after.
    Returns `text` unchanged if it doesn't open with an intro heading at
    all."""
    m = _COMPANY_INTRO_HEADING_RE.match(text)
    if not m:
        return text
    sentences = _SENTENCE_SPLIT_RE.split(text[m.end():])
    i = 0
    real_skipped = 0
    while i < len(sentences) and real_skipped < _MAX_INTRO_SENTENCES_SKIPPED:
        s = sentences[i]
        # Naive sentence-splitting on ". " breaks on abbreviations like
        # "Digible, Inc." too, producing short junk fragments that aren't
        # real sentences at all — skip past those for free without
        # spending the marketing-signal budget on them, rather than letting
        # them prematurely stop the loop before reaching real content.
        if len(s) < 25:
            i += 1
            continue
        if real_skipped == 0 or _COMPANY_MARKETING_SIGNAL_RE.search(s):
            i += 1
            real_skipped += 1
            continue
        break
    return " ".join(sentences[i:])


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
    field. Priority: (1) bullets from the first section whose title looks
    like a qualifications/responsibilities heading — real bullet content is
    more specific and useful than a single sentence, so it goes first now;
    (2) a years-of-experience sentence, if bullets came up empty; (3) the
    first substantive sentence of the first section with real content,
    skipping past an "About [Company]" intro if that's what the section
    opens with."""
    if not sections:
        return ""

    for title, content in sections:
        if _is_quals_heading(title):
            bullets = _bullets_from_li_blob(unescape_repeated(content or ""))
            if bullets:
                return _truncate(_clean(bullets))

    for _title, content in sections:
        plain = strip_html(content or "")
        ctx = _years_match_with_context(plain)
        if ctx:
            return _truncate(_clean(ctx))

    # Nothing quals-shaped or years-mentioned; describe the role from the
    # first section with real content (skip section headings that are just
    # the job title repeated back, which Lever includes as the first list
    # item, and skip past an About-company intro rather than using it).
    for _title, content in sections:
        plain = _clean(strip_html(content or ""))
        plain = _skip_about_block(plain)
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
    use extract_blurb_from_sections() instead.

    Priority: (1) bullets under a qualifications/responsibilities-style
    heading — the most specific, useful signal; (2) a years-of-experience
    sentence, if no bullets were found; (3) the first substantive sentence
    of the description, actively skipping an "About [Company]" intro
    paragraph if that's what the posting opens with (it's boilerplate
    repeated across every posting from that company, not a description of
    THIS role — see the module docstring for the real example that caught
    this)."""
    if not html_or_text or len(html_or_text) < 40:
        return ""

    plain = strip_html(html_or_text) if is_html else html_or_text

    if is_html:
        # unescape (possibly repeatedly) first — some sources double-escape
        # their HTML, so the tags _HEADING_TAG_RE looks for may only exist
        # as literal "&lt;h2&gt;" text otherwise, never matching anything.
        bullets = _bullets_after_heading_in_blob(unescape_repeated(html_or_text))
        if bullets:
            return _truncate(_clean(bullets))

    ctx = _years_match_with_context(plain)
    if ctx:
        return _truncate(_clean(ctx))

    cleaned = _skip_about_block(_clean(plain))
    sentences = _SENTENCE_SPLIT_RE.split(cleaned)
    for s in sentences:
        if len(s) >= 60:
            return _truncate(s)

    return _truncate(cleaned) if cleaned.strip() else ""
