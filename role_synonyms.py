"""
role_synonyms.py — curated synonym groups for common white-collar role
families, so a resume that says "Revenue Operations Manager" also matches
job postings titled "RevOps Manager", "Sales Operations", "GTM Operations",
etc. — real job titles for functionally-the-same work vary a lot more
across companies than resume language does.

This is a hand-curated dictionary, not an ML model or embedding lookup — it
covers common tech/business role families (the ones a general audience of
job seekers is most likely to search), not every possible job title in
existence. Expand ROLE_SYNONYM_GROUPS as gaps show up; there's no mechanism
here for auto-discovering new ones.
"""

# Each group: "triggers" = substrings that, if found in an extracted resume
# phrase, activate this group; "related" = terms added to the result when
# triggered (the trigger phrases themselves don't need to be repeated here,
# extract_with_synonyms() already keeps the original phrase).
ROLE_SYNONYM_GROUPS = [
    {
        "triggers": ["revenue operations", "revops", "rev ops", "sales operations", "sales ops",
                     "gtm operations", "go-to-market operations", "commercial operations"],
        "related": ["Revenue Operations", "RevOps", "Rev Ops", "Sales Operations", "Sales Ops",
                     "GTM", "Go-to-Market", "Business Operations", "Deal Desk"],
    },
    {
        "triggers": ["business operations", "biz ops", "bizops", "strategy & operations",
                     "strategy and operations", "strategy & business operations"],
        # "Chief of Staff" and "Special Projects" added here (not just on the
        # chief-of-staff trigger below) so the expansion works in both
        # directions: a resume titled "Chief of Staff" already pulled in
        # Business Operations via the group below, but a resume titled
        # "Director, Strategy & Business Operations" was NOT pulling in
        # "Chief of Staff" postings, even though that's a well-established
        # real-world adjacency (BizOps/Strategy leaders regularly move into
        # or apply for Chief of Staff roles, and vice versa).
        "related": ["Business Operations", "BizOps", "Biz Ops", "Strategy & Operations",
                     "Strategy and Operations", "Operations", "Chief of Staff",
                     "Special Projects"],
    },
    {
        "triggers": ["chief of staff"],
        "related": ["Chief of Staff", "Business Operations", "Strategy & Operations",
                     "Special Projects"],
    },
    {
        "triggers": ["product manager", "product owner", "product lead", "product management"],
        "related": ["Product Manager", "Product Owner", "PM", "Product Lead",
                     "Technical Product Manager", "Product Management"],
    },
    {
        "triggers": ["program manager", "project manager", "pmo", "project management",
                     "program management"],
        "related": ["Program Manager", "Project Manager", "PMO", "Technical Program Manager",
                     "TPM", "Project Management"],
    },
    {
        "triggers": ["growth marketing", "demand generation", "demand gen", "performance marketing",
                     "digital marketing", "marketing manager"],
        "related": ["Growth Marketing", "Demand Generation", "Demand Gen", "Performance Marketing",
                     "Growth", "Marketing Manager", "Digital Marketing"],
    },
    {
        "triggers": ["product marketing"],
        "related": ["Product Marketing", "PMM", "Go-to-Market", "GTM"],
    },
    {
        "triggers": ["account executive", "business development", "sales representative",
                     "sales rep", "bdr", "sdr", "outside sales", "inside sales"],
        "related": ["Account Executive", "AE", "Business Development", "BDR", "SDR",
                     "Sales Development Representative", "Business Development Representative",
                     "Sales Representative"],
    },
    {
        "triggers": ["customer success", "csm", "account management", "client success",
                     "customer experience"],
        "related": ["Customer Success", "CSM", "Customer Success Manager", "Account Management",
                     "Client Success", "Customer Experience"],
    },
    {
        "triggers": ["data analyst", "business analyst", "analytics", "business intelligence",
                     "data analytics"],
        "related": ["Data Analyst", "Business Analyst", "Analytics", "Business Intelligence", "BI",
                     "Data Analytics"],
    },
    {
        "triggers": ["data scientist", "machine learning", "ml engineer", "data science"],
        "related": ["Data Scientist", "Machine Learning Engineer", "ML Engineer", "Data Science",
                     "Applied Scientist"],
    },
    {
        "triggers": ["software engineer", "swe", "backend engineer", "frontend engineer",
                     "full stack", "fullstack", "software developer"],
        "related": ["Software Engineer", "SWE", "Backend Engineer", "Frontend Engineer",
                     "Full Stack Engineer", "Software Developer"],
    },
    {
        "triggers": ["ux designer", "ui designer", "product designer", "user experience",
                     "user interface"],
        "related": ["Product Designer", "UX Designer", "UI Designer", "UX/UI Designer",
                     "User Experience Designer"],
    },
    {
        "triggers": ["human resources", "people operations", "people ops", "hr generalist",
                     "hr business partner", "hrbp"],
        "related": ["Human Resources", "HR", "People Operations", "People Ops", "HRBP",
                     "HR Business Partner", "People & Culture"],
    },
    {
        "triggers": ["talent acquisition", "recruiter", "recruiting", "technical recruiter"],
        "related": ["Talent Acquisition", "Recruiter", "Recruiting", "Technical Recruiter",
                     "Talent Partner"],
    },
    {
        "triggers": ["financial planning", "fp&a", "finance manager", "financial analyst"],
        "related": ["FP&A", "Financial Planning & Analysis", "Finance Manager",
                     "Financial Analyst", "Finance"],
    },
    {
        "triggers": ["accounting", "accountant", "controller", "bookkeeper"],
        "related": ["Accounting", "Accountant", "Controller", "Staff Accountant"],
    },
    {
        "triggers": ["supply chain", "logistics", "procurement", "sourcing"],
        "related": ["Supply Chain", "Logistics", "Procurement", "Sourcing",
                     "Supply Chain Manager"],
    },
    {
        "triggers": ["operations manager", "ops manager", "operations analyst"],
        "related": ["Operations Manager", "Ops Manager", "Operations Analyst", "Operations"],
    },
    {
        "triggers": ["strategy", "corporate strategy", "strategic planning", "strategy consultant"],
        "related": ["Strategy", "Corporate Strategy", "Strategic Planning", "Strategy & Operations"],
    },
    {
        "triggers": ["consultant", "management consulting", "consulting"],
        "related": ["Consultant", "Management Consulting", "Consulting"],
    },
    {
        "triggers": ["it support", "systems administrator", "sysadmin", "it manager",
                     "network administrator"],
        "related": ["IT Support", "Systems Administrator", "SysAdmin", "IT Manager",
                     "Network Administrator"],
    },
    {
        "triggers": ["legal counsel", "attorney", "paralegal", "compliance"],
        "related": ["Legal Counsel", "Attorney", "Paralegal", "Compliance", "Corporate Counsel"],
    },
    {
        "triggers": ["executive assistant", "administrative assistant", "office manager"],
        "related": ["Executive Assistant", "EA", "Administrative Assistant", "Office Manager"],
    },
]

_STOP_TRIGGERS = None  # lazily built: sorted longest-first so "revenue operations" is
                       # checked before a shorter substring accidentally matching first


def _compiled_groups():
    global _STOP_TRIGGERS
    if _STOP_TRIGGERS is None:
        _STOP_TRIGGERS = [
            (sorted(g["triggers"], key=len, reverse=True), g["related"])
            for g in ROLE_SYNONYM_GROUPS
        ]
    return _STOP_TRIGGERS


def expand_with_synonyms(phrases, max_extra=20):
    """Given a list of extracted resume phrases, return a NEW list of
    additional related terms (not including the originals) pulled from any
    matching synonym group, deduped and capped at max_extra."""
    seen_lower = {p.lower() for p in phrases}
    extra = []
    for phrase in phrases:
        pl = phrase.lower()
        for triggers, related in _compiled_groups():
            if any(trig in pl for trig in triggers):
                for rel in related:
                    if rel.lower() not in seen_lower:
                        seen_lower.add(rel.lower())
                        extra.append(rel)
                        if len(extra) >= max_extra:
                            return extra
    return extra
