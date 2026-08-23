"""
resume_parser.py — pull a rough search query out of an uploaded resume.

Not trying to be a full resume-parsing NLP pipeline: it (1) looks for an
explicit Skills/Core Competencies section and lifts its items verbatim, and
(2) regexes for "<Title Case words> + <role noun>" phrases like "Senior
Product Manager" or "Registered Nurse" and takes the most frequent ones.
(3) expands extracted title phrases through role_synonyms.py, since real
job postings for the same kind of work often use a completely different
title than a resume does ("Revenue Operations Manager" on a resume vs.
"RevOps Manager" / "Sales Operations" / "GTM" on job boards) — keyword
search against the literal resume phrase alone misses those.
Everything gets combined into a ready-to-edit boolean query string, plus a
separate (longer) term list used for match-tier scoring against job cards.
"""

import io
import re
from collections import Counter

from role_synonyms import expand_with_synonyms
from metro_areas import find_metro_area

ROLE_NOUNS = [
    "manager", "director", "engineer", "analyst", "specialist", "coordinator",
    "lead", "consultant", "officer", "executive", "associate", "administrator",
    "architect", "designer", "developer", "scientist", "strategist",
    "supervisor", "representative", "nurse", "technician", "recruiter",
    "accountant", "attorney", "counsel", "planner", "buyer", "chef", "teacher",
    "instructor", "physician", "therapist", "paralegal", "pharmacist",
    "controller", "bookkeeper", "underwriter", "actuary", "producer",
    "editor", "journalist", "photographer", "electrician", "plumber",
    "mechanic", "welder", "machinist", "paramedic", "dietitian", "counselor",
    "ops", "operations", "partner",
]
ROLE_NOUN_RE = re.compile(
    # The leading "Title Case words" part is deliberately case-SENSITIVE
    # (genuinely requires a capital first letter, no re.IGNORECASE on this
    # part) — only the role-noun alternation itself is case-insensitive via
    # the scoped (?i:...) group. With the whole pattern case-insensitive,
    # ANY word (including "with", "and", "the") satisfies `[A-Z]`, so a
    # sentence like "Partnered with Sales Ops" matched as a 3-word phrase
    # instead of just "Sales Ops". Requiring real capitalization for the
    # leading words means a lowercase word breaks the run, so the match
    # correctly starts at the next actually-capitalized word instead.
    r"\b((?:[A-Z][a-zA-Z/&\-]*\s+){0,3}(?i:" + "|".join(ROLE_NOUNS) + r"))\b"
)

# Common resume-summary buzzwords that are capitalized only because they
# happen to start a sentence ("Results-driven Revenue Operations Manager
# with..."), not because they're part of an actual title — dropped the same
# way SECTION_HEADER_WORDS gets dropped below.
BUZZWORD_RE = re.compile(
    r"^(results?-?driven|experienced|skilled|proven|dynamic|motivated|"
    r"passionate|detail-?oriented|highly|seasoned|accomplished|dedicated|"
    r"driven|innovative|self-?motivated|hard-?working|goal-?oriented|"
    r"versatile|adaptable|enthusiastic)$",
    re.IGNORECASE,
)

SKILLS_HEADER_RE = re.compile(
    r"(?im)^\s*(technical\s+skills|core\s+competencies|skills(?:\s*&\s*tools)?|"
    r"tools?\s*(?:&|and)?\s*technologies|technical\s+proficienc(?:y|ies)|areas\s+of\s+expertise)"
    r"\s*:?\s*$"
)

STOPWORDS = {
    "the", "and", "or", "a", "an", "of", "in", "on", "for", "to", "with",
    "at", "by", "is", "are", "as", "from",
}


def extract_text(file_bytes, filename):
    """Best-effort text extraction for .pdf, .docx, or .txt uploads."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return _extract_pdf(file_bytes)
    if name.endswith(".docx"):
        return _extract_docx(file_bytes)
    # fall back to treating it as plain text
    return file_bytes.decode("utf-8", errors="replace")


def _extract_pdf(file_bytes):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(file_bytes):
    import docx
    doc = docx.Document(io.BytesIO(file_bytes))
    return "\n".join(p.text for p in doc.paragraphs)


def _extract_skills_section(text):
    lines = text.splitlines()
    items = []
    for i, line in enumerate(lines):
        if SKILLS_HEADER_RE.match(line.strip()):
            # take the next ~15 non-empty lines, or until a line that looks like a new ALL-CAPS header
            for j in range(i + 1, min(i + 16, len(lines))):
                nxt = lines[j].strip()
                if not nxt:
                    if items:
                        break
                    continue
                if nxt.isupper() and len(nxt.split()) <= 5:
                    break  # looks like the next section header
                parts = re.split(r"[,;|•●–—•·]+", nxt)
                for p in parts:
                    p = p.strip(" \t-•")
                    if 2 <= len(p) <= 40 and p.lower() not in STOPWORDS:
                        items.append(p)
            break
    return items


# "City, ST" pattern — resume contact-info blocks put this near the very top
# ("Jane Doe | Phoenix, AZ | jane@email.com"), so we only search the first
# chunk of the document first and fall back to a whole-document search only
# if that comes up empty. Restricting to a real 2-letter USPS state code
# (case-sensitive, since e.g. "Ma" or "In" are ordinary words) keeps this
# from firing on unrelated capitalized word pairs.
CITY_STATE_RE = re.compile(
    # City words are joined by a plain space/hyphen ONLY (not \s, which
    # includes newlines) — otherwise a name-then-city on consecutive resume
    # header lines like "Jane Doe\nPhoenix, AZ" gets swallowed into one
    # bogus "Jane Doe Phoenix" match.
    r"\b([A-Z][a-zA-Z.]+(?:[ \t-][A-Z][a-zA-Z.]+){0,2}),[ \t]*"
    r"(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
    r"MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|"
    r"UT|VT|VA|WA|WV|WI|WY|DC)\b"
)
RESUME_HEADER_CHARS = 600


def extract_location(text):
    """Best-effort 'City, ST' extraction, biased toward the top of the
    document where contact info usually lives. Returns (nearby_locations,
    matched_label): if the city is in our curated metro dataset,
    nearby_locations is the list of nearby cities/suburbs to also search;
    otherwise it's just the single "City, ST" string on its own. Returns
    ([], None) if no city/state pair is found at all."""
    m = CITY_STATE_RE.search(text[:RESUME_HEADER_CHARS]) or CITY_STATE_RE.search(text)
    if not m:
        return [], None
    city, state = m.group(1).strip(), m.group(2).upper()
    label = f"{city}, {state}"
    nearby = find_metro_area(city, state)
    return (nearby if nearby else [label]), label


SECTION_HEADER_WORDS = {
    "experience", "education", "skills", "summary", "objective", "profile",
    "projects", "certifications", "awards", "publications", "references",
    "employment", "history", "work",
}


def _extract_title_phrases(text):
    counts = Counter()
    # match per line so phrases never bridge across a section-header line break
    for line in text.splitlines():
        for m in ROLE_NOUN_RE.finditer(line):
            phrase = re.sub(r"\s+", " ", m.group(1)).strip()
            words = phrase.split()
            # drop a leading word that's actually a section header bleeding in,
            # or a resume-summary buzzword that's capitalized only because it
            # starts a sentence ("Results-driven Revenue Operations Manager"),
            # not because it's genuinely part of the title
            while words and (words[0].lower() in SECTION_HEADER_WORDS or BUZZWORD_RE.match(words[0])):
                words = words[1:]
            phrase = " ".join(words)
            if len(phrase) < 4 or len(words) > 4:
                continue
            counts[phrase] += 1
    # keep the most frequent, prefer longer/more specific phrases on ties
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    return [phrase for phrase, _ in ranked[:8]]


def suggest_query(text, max_query_terms=8, max_terms=25):
    """Returns (terms, query_string).

    `terms` is the fuller list — extracted title phrases, skills, AND
    role-synonym expansions (e.g. "Revenue Operations Manager" also pulls in
    "RevOps", "Sales Ops", "GTM") — returned for display and used as the
    match-scoring vocabulary against job cards. It's deliberately longer
    than what goes into the query box: cluttering the editable search query
    with 20+ OR'd synonyms would make it unreadable and unpredictable to
    edit, but for match-tier scoring, more real synonyms = a better signal,
    not a worse one.

    `query_string` is built from only the first `max_query_terms` originally-
    extracted terms (titles + skills, NOT synonym expansions) — kept short
    and legible since the user is expected to review/edit it before
    searching.
    """
    titles = _extract_title_phrases(text)
    skills = _extract_skills_section(text)

    seen = set()
    base_terms = []
    for t in titles + skills:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        base_terms.append(t)

    synonyms = expand_with_synonyms(titles, max_extra=max_terms)

    terms = list(base_terms)
    for s in synonyms:
        if s.lower() not in seen:
            seen.add(s.lower())
            terms.append(s)
        if len(terms) >= max_terms:
            break

    query_terms = base_terms[:max_query_terms]
    parts = [f'"{t}"' if " " in t else t for t in query_terms]
    query_string = " OR ".join(parts)
    return terms, query_string
