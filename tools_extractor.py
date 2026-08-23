"""
tools_extractor.py — pull the specific tools/technologies a posting
mentions (Salesforce, SQL, Figma, Kubernetes, ...) so a job card can show
them as their own compact row, the way hiring.cafe's cards do (a wrench-icon
line under the qualifications blurb). Same curated-list-plus-regex-matching
approach as role_synonyms.py and location_groups.py: this can't be derived
from a heading/structure the way the blurb bullets are, since tool mentions
are scattered inline through the description text, not under a predictable
heading — so it's pattern matching against a hand-picked list of real
tool/technology names spanning the many job functions this app covers
(sales/GTM, data, engineering, design, ops/HR/finance), not just tech roles.

Deliberately excludes single-letter or very short/ambiguous tokens that
would false-positive constantly in ordinary English (bare "R", bare "Go",
bare "C") — same judgment call as excluding ambiguous short location
abbreviations elsewhere in this app. A few short-but-safe ones (SQL, AWS,
GCP, API) are kept since they're not real English words and unlikely to
appear by coincidence.
"""

import re

# (display name, list of literal phrases that count as a match — usually
# just the name itself, occasionally a couple of common spellings)
TOOLS = [
    ("Salesforce", ["salesforce"]),
    ("HubSpot", ["hubspot"]),
    ("Slack", ["slack"]),
    ("Zoom", ["zoom"]),
    ("Notion", ["notion"]),
    ("Asana", ["asana"]),
    ("Jira", ["jira"]),
    ("Confluence", ["confluence"]),
    ("Google Workspace", ["google workspace", "google suite", "g suite"]),
    ("Microsoft Excel", ["excel"]),
    ("PowerPoint", ["powerpoint"]),
    ("Tableau", ["tableau"]),
    ("Looker", ["looker"]),
    ("Power BI", ["power bi"]),
    ("SQL", ["sql"]),
    ("Python", ["python"]),
    ("AWS", ["aws", "amazon web services"]),
    ("GCP", ["gcp", "google cloud"]),
    ("Azure", ["microsoft azure", "azure"]),
    ("Kubernetes", ["kubernetes", "k8s"]),
    ("Docker", ["docker"]),
    ("React", ["react.js", "reactjs", "react"]),
    ("Node.js", ["node.js", "nodejs"]),
    ("TypeScript", ["typescript"]),
    ("JavaScript", ["javascript"]),
    ("Java", ["java"]),
    ("Figma", ["figma"]),
    ("Adobe Creative Suite", ["adobe creative", "photoshop", "illustrator"]),
    ("Zendesk", ["zendesk"]),
    ("Intercom", ["intercom"]),
    ("NetSuite", ["netsuite"]),
    ("Workday", ["workday"]),
    ("SAP", ["sap"]),
    ("QuickBooks", ["quickbooks"]),
    ("Marketo", ["marketo"]),
    ("Mailchimp", ["mailchimp"]),
    # "Segment" bare is deliberately excluded -- "market segment", "customer
    # segment", "audience segment" are extremely common in exactly the
    # sales/marketing postings this app scrapes a lot of, so bare "segment"
    # would false-positive constantly. Only the distinctive product name.
    ("Segment", ["segment.io", "segment cdp", "twilio segment"]),
    ("Amplitude", ["amplitude"]),
    ("Mixpanel", ["mixpanel"]),
    ("Snowflake", ["snowflake"]),
    ("dbt", ["dbt"]),
    ("Airflow", ["airflow"]),
    ("Terraform", ["terraform"]),
    ("Git/GitHub", ["github", "git"]),
    ("Linux", ["linux"]),
    ("Oracle", ["oracle"]),
    ("SharePoint", ["sharepoint"]),
    ("Miro", ["miro"]),
    ("Whimsical", ["whimsical"]),
    # "Linear" (the issue tracker) bare is excluded -- "linear regression",
    # "linear algebra", "linear model" are common statistics/data-science
    # terms that would false-positive on unrelated postings.
    ("Linear", ["linear.app", "linear issue tracker"]),
    ("Monday.com", ["monday.com"]),
    ("ServiceNow", ["servicenow"]),
    ("Greenhouse (ATS)", ["greenhouse ats", "greenhouse recruiting"]),
    ("Gong", ["gong.io", "gong"]),
    ("Outreach", ["outreach.io"]),
    ("DocuSign", ["docusign"]),
    ("SQL Server", ["sql server"]),
    ("R (statistics)", ["r programming", "r studio", "rstudio"]),
    ("AI/LLM", ["large language model", "llm", "generative ai", "chatgpt", "openai api"]),
    ("RAG", ["retrieval-augmented generation", "retrieval augmented generation", "rag "]),
    ("Machine Learning", ["machine learning", "ml models", "ml pipeline"]),
    ("API integrations", ["apis", "api integrations", "rest api", "restful api"]),
]

# Case-insensitive whole-phrase matching against plain (tag-stripped) text.
# Compiled once at import time.
_COMPILED = [
    (name, [re.compile(r"\b" + re.escape(phrase.strip()) + r"\b", re.IGNORECASE) for phrase in phrases])
    for name, phrases in TOOLS
]


def extract_tools(text, limit=6):
    """Returns up to `limit` distinct tool/technology display names found
    in `text` (plain, tag-stripped), in the order the TOOLS list is
    defined (roughly business-tools-first, then data, then engineering) —
    not order of appearance in the text, so results are stable and the
    most broadly-relevant tools surface first when a posting mentions a
    lot of them."""
    if not text:
        return []
    found = []
    for name, patterns in _COMPILED:
        if any(p.search(text) for p in patterns):
            found.append(name)
            if len(found) >= limit:
                break
    return found
