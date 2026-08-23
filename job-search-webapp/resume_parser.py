"""
resume_parser.py — pull a rough search query out of an uploaded resume.

Not trying to be a full resume-parsing NLP pipeline: it (1) looks for an
explicit Skills/Core Competencies section and lifts its items verbatim, and
(2) regexes for "<Title Case words> + <role noun>" phrases like "Senior
Product Manager" or "Registered Nurse" and takes the most frequent ones.
Both lists get combined into a ready-to-edit boolean query string.
"""

import io
import re
from collections import Counter

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
]
ROLE_NOUN_RE = re.compile(
    r"\b((?:[A-Z][a-zA-Z/&\-]*\s+){0,3}(?:" + "|".join(ROLE_NOUNS) + r"))\b",
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
            # drop a leading word that's actually a section header bleeding in
            if words and words[0].lower() in SECTION_HEADER_WORDS:
                words = words[1:]
            phrase = " ".join(words)
            if len(phrase) < 4 or len(words) > 4:
                continue
            counts[phrase] += 1
    # keep the most frequent, prefer longer/more specific phrases on ties
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    return [phrase for phrase, _ in ranked[:6]]


def suggest_query(text, max_terms=10):
    """Returns (terms, query_string). `terms` is the raw extracted list for
    display; `query_string` is an OR-joined boolean query ready to drop into
    the search box (quoted where the term has more than one word)."""
    titles = _extract_title_phrases(text)
    skills = _extract_skills_section(text)

    seen = set()
    terms = []
    for t in titles + skills:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(t)
        if len(terms) >= max_terms:
            break

    parts = []
    for t in terms:
        parts.append(f'"{t}"' if " " in t else t)
    query_string = " OR ".join(parts)
    return terms, query_string
