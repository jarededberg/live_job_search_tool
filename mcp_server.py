"""
mcp_server.py — a remote MCP (Model Context Protocol) server exposing this
site's job search as tools an AI agent can call directly, over plain HTTP,
without anyone needing to install or configure anything locally.

Protocol version chosen: 2025-06-18, not the brand-new 2026-07-28 revision.
The 2026-07-28 spec is a genuinely large breaking change (it removes
protocol-level sessions and the `initialize` handshake entirely, in favor of
a fully stateless per-request model) and was still labeled a "Release
Candidate" as of its own announcement -- real MCP clients (Claude Desktop,
Claude.ai custom connectors, third-party agent frameworks) take months to
catch up to a change that size. Building strictly to the newest spec would
mean a server that's "more correct" on paper but that most people's actual
AI tools can't talk to today. 2025-06-18's initialize+tools/list+tools/call
flow is the version that's been stable and broadly implemented for over a
year, so that's the target here. The dispatch table below is deliberately
just a dict of method name -> handler, so upgrading to the newer stateless
flow later (or supporting both, per the spec's own backward-compatibility
guidance) is a small, additive change rather than a rewrite.

This module is transport-agnostic on purpose: `handle_request()` takes a
parsed JSON-RPC message (a dict) and returns either a response dict (for a
request) or None (for a notification, which gets no body at all per spec --
see the Streamable HTTP transport's rules for notification POSTs). app.py's
/mcp route owns the actual HTTP mechanics (status codes, rate limiting,
JSON parsing) and just calls into this.
"""

import os
import re

import db

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "skip-the-boards"
SERVER_VERSION = "1.0.0"

# Same fallback pattern as app.py's SITE_URL -- reuses APP_BASE_URL if set
# (Render/Cloudflare terminate TLS in front of this app, so there's no
# reliable way to derive this from the request itself), otherwise the real
# production domain. Duplicated here rather than imported from app.py
# specifically to avoid a circular import (app.py imports this module to
# register the /mcp route).
SITE_URL = os.environ.get("APP_BASE_URL") or "https://skiptheboards.com"

MAX_PER_PAGE = 20
DEFAULT_PER_PAGE = 10

VALID_LOCATION_GROUPS = {
    "remote_us", "remote_canada", "remote_uk", "remote_europe", "remote_anywhere",
}


def _slugify(text):
    """Same rule as app.py's _slugify() (kept in sync by hand, not
    imported, for the same circular-import reason as SITE_URL above) --
    used to build a company's /jobs/company/<slug> hub URL in tool
    results."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:80] or "role"


def _job_url(job):
    """Public detail-page URL for a job -- same <job_id>-<slug> shape as
    app.py's _job_path(), rebuilt here rather than imported for the same
    reason as _slugify above."""
    slug = _slugify(f"{job.get('title', '')}-{job.get('company', '')}")
    return f"{SITE_URL}/jobs/{job.get('job_id', '')}-{slug}"


def _company_url(company):
    return f"{SITE_URL}/jobs/company/{_slugify(company)}"


def _job_summary(job):
    """Compact job dict for structuredContent -- deliberately a subset of
    the full row (no raw scrape metadata, no internal ids beyond job_id
    itself), matching what a model actually needs to describe a listing
    or decide whether to fetch full details."""
    salary = None
    if job.get("salary_min"):
        salary = {
            "min": job["salary_min"],
            "max": job.get("salary_max") or job["salary_min"],
            "currency": "USD",
        }
    return {
        "job_id": job.get("job_id"),
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location") or None,
        "department": job.get("department") or None,
        "commitment": job.get("commitment") or None,
        "salary": salary,
        "years_experience": job.get("years_experience") or None,
        "posted": (job.get("posted") or "")[:10] or None,
        "apply_url": job.get("url"),
        "detail_url": _job_url(job),
    }


# ---------------------------------------------------------------------------
# Tool definitions + implementations
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_jobs",
        "description": (
            "Search live job postings pulled directly from roughly 4,300 companies' "
            "own Greenhouse, Lever, or Ashby career pages (not re-posted from another "
            "job board, so nothing here is stale). Supports boolean title search "
            "(AND/OR/NOT, quoted phrases, parentheses) plus location/department/"
            "commitment/recency filters. Returns a page of matching jobs with each "
            "job's own detail-page URL and its direct external apply URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Boolean search against job titles. Bare words are an implicit "
                        "AND. Supports OR, NOT, quoted exact phrases, and parentheses "
                        "for grouping, e.g. '(\"product manager\" OR \"product owner\") "
                        "NOT senior'. Leave empty to browse without a title filter."
                    ),
                },
                "location": {
                    "type": "string",
                    "description": (
                        "Free-text substring match against the job's raw location "
                        "string, e.g. 'Austin' or 'New York'. Leave empty for no "
                        "location filter."
                    ),
                },
                "location_group": {
                    "type": "string",
                    "enum": sorted(VALID_LOCATION_GROUPS),
                    "description": (
                        "Canonical remote-region filter, collapsing the dozens of raw "
                        "spellings companies use for 'remote' into one option. Combined "
                        "with `location` via OR if both are given."
                    ),
                },
                "department": {
                    "type": "string",
                    "description": "Substring match against the job's department/team, e.g. 'Engineering' or 'Sales'.",
                },
                "commitment": {
                    "type": "string",
                    "description": "Exact match against employment type as posted, e.g. 'Full-time', 'Contract', 'Internship'.",
                },
                "days": {
                    "type": "integer",
                    "description": "Only include jobs posted within this many days.",
                    "minimum": 1,
                },
                "sort": {
                    "type": "string",
                    "enum": ["newest", "oldest", "company", "salary"],
                    "description": "Sort order. Defaults to newest.",
                },
                "page": {
                    "type": "integer",
                    "description": "1-indexed page number. Defaults to 1.",
                    "minimum": 1,
                },
                "per_page": {
                    "type": "integer",
                    "description": f"Results per page, up to {MAX_PER_PAGE}. Defaults to {DEFAULT_PER_PAGE}.",
                    "minimum": 1,
                    "maximum": MAX_PER_PAGE,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_job_details",
        "description": (
            "Full detail for a single job by its job_id (returned by search_jobs), "
            "including the full role blurb/qualifications text, listed tools/tech, "
            "and both the site's own detail-page URL and the direct external apply URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The 12-character job_id from a search_jobs result.",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_companies",
        "description": (
            "Find companies in the index by name (substring match) that currently "
            "have at least one open role, along with how many roles they have open "
            "and a link to that company's full job listing page on the site. Useful "
            "when someone asks about openings 'at <company>' rather than by role."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring to match against company names, e.g. 'block' or 'anthropic'.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]


def _tool_search_jobs(args):
    query = (args.get("query") or "").strip()
    location = (args.get("location") or "").strip()
    location_group = (args.get("location_group") or "").strip()
    department = (args.get("department") or "").strip()
    commitment = (args.get("commitment") or "").strip()
    days = args.get("days")
    sort = args.get("sort") or "newest"
    page = args.get("page") or 1
    per_page = min(int(args.get("per_page") or DEFAULT_PER_PAGE), MAX_PER_PAGE)

    if location_group and location_group not in VALID_LOCATION_GROUPS:
        return _tool_error(f"Unknown location_group '{location_group}'. Valid values: {sorted(VALID_LOCATION_GROUPS)}")

    location_groups = [location_group] if location_group else None

    jobs, total = db.search_jobs(
        query=query,
        location=location,
        location_groups=location_groups,
        department=department,
        commitment=commitment,
        days=days,
        sort=sort,
        page=page,
        per_page=per_page,
    )

    summaries = [_job_summary(j) for j in jobs]
    if not summaries:
        text = "No jobs matched that search."
    else:
        lines = [f"Found {total} matching job(s), showing {len(summaries)}:"]
        for s in summaries:
            loc = f" ({s['location']})" if s["location"] else ""
            lines.append(f"- {s['title']} at {s['company']}{loc} -- {s['detail_url']}")
        text = "\n".join(lines)

    return _tool_ok(text, {"total": total, "page": page, "per_page": per_page, "jobs": summaries})


def _tool_get_job_details(args):
    job_id = (args.get("job_id") or "").strip()
    if not job_id:
        return _tool_error("job_id is required.")
    job = db.get_job_by_job_id(job_id)
    if job is None:
        return _tool_error(f"No job found with job_id '{job_id}'. It may have closed and dropped out of the index.")

    summary = _job_summary(job)
    blurb = job.get("blurb") or ""
    tools = job.get("tools") or []
    lines = [f"{summary['title']} at {summary['company']}"]
    if summary["location"]:
        lines.append(f"Location: {summary['location']}")
    if summary["salary"]:
        s = summary["salary"]
        lines.append(f"Salary: ${s['min']:,}-${s['max']:,} {s['currency']}" if s["max"] != s["min"] else f"Salary: ${s['min']:,} {s['currency']}")
    if summary["years_experience"]:
        lines.append(f"Experience: {summary['years_experience']}")
    if tools:
        lines.append(f"Tools/tech mentioned: {', '.join(tools)}")
    if blurb:
        lines.append(f"\n{blurb}")
    lines.append(f"\nApply directly: {summary['apply_url']}")
    lines.append(f"Full listing: {summary['detail_url']}")

    structured = dict(summary)
    structured["blurb"] = blurb
    structured["tools"] = tools
    return _tool_ok("\n".join(lines), structured)


def _tool_search_companies(args):
    query = (args.get("query") or "").strip().lower()
    if not query:
        return _tool_error("query is required.")
    companies = db.list_companies_with_open_jobs()
    matches = [c for c in companies if query in c["company"].lower()][:15]
    if not matches:
        text = f"No companies matching '{query}' currently have open roles."
    else:
        lines = [f"{len(matches)} matching compan{'y' if len(matches) == 1 else 'ies'}:"]
        for c in matches:
            lines.append(f"- {c['company']} ({c['count']} open role{'s' if c['count'] != 1 else ''}) -- {_company_url(c['company'])}")
        text = "\n".join(lines)
    structured = [
        {"company": c["company"], "open_roles": c["count"], "url": _company_url(c["company"])}
        for c in matches
    ]
    return _tool_ok(text, structured)


TOOL_HANDLERS = {
    "search_jobs": _tool_search_jobs,
    "get_job_details": _tool_get_job_details,
    "search_companies": _tool_search_companies,
}


def _tool_ok(text, structured_content):
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured_content, "isError": False}


def _tool_error(message):
    """Tool EXECUTION error (not a protocol error) -- reported inside a
    normal tools/call result with isError: true, per spec, since this is
    the kind of thing a model can see and self-correct from (bad job_id,
    unknown location_group), not a malformed request."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------------------
# JSON-RPC method dispatch
# ---------------------------------------------------------------------------

def _rpc_result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _handle_initialize(msg_id, params):
    return _rpc_result(msg_id, {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "Search live job postings from ~4,300 companies' own Greenhouse/Lever/"
            "Ashby career pages. Use search_jobs to find roles, get_job_details for "
            "the full listing text, and search_companies to find a specific "
            "employer's open roles."
        ),
    })


def _handle_tools_list(msg_id, params):
    return _rpc_result(msg_id, {"tools": TOOLS})


def _handle_tools_call(msg_id, params):
    name = (params or {}).get("name")
    arguments = (params or {}).get("arguments") or {}
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return _rpc_error(msg_id, -32602, f"Unknown tool: {name}")
    try:
        result = handler(arguments)
    except Exception as e:
        # A bug in our own handler is still a tool execution error from the
        # caller's point of view, not a protocol error -- report it the
        # same way rather than letting a 500 bubble up and killing the
        # whole request for an otherwise well-formed call.
        result = _tool_error(f"Internal error handling '{name}': {e}")
    return _rpc_result(msg_id, result)


def _handle_ping(msg_id, params):
    return _rpc_result(msg_id, {})


METHODS = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": _handle_ping,
}

# Client notifications (no `id`, no response expected) that we recognize
# and silently accept -- most importantly `notifications/initialized`,
# which every conforming client sends right after a successful
# `initialize` response and before any other request.
NOTIFICATION_METHODS = {"notifications/initialized", "notifications/cancelled"}


def handle_request(message):
    """Dispatches one parsed JSON-RPC message. Returns a response dict for
    a request, or None for a notification (caller should respond with a
    bare 202 Accepted and no body -- see Streamable HTTP transport rules).
    Raises ValueError for a structurally invalid JSON-RPC message (caller
    should turn that into a 400 + Parse/Invalid Request error)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise ValueError("Invalid JSON-RPC message: missing or wrong 'jsonrpc' field")

    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params")

    if method is None:
        raise ValueError("Invalid JSON-RPC message: missing 'method'")

    is_notification = "id" not in message

    if is_notification:
        # Recognized or not, a notification never gets a body -- an
        # unrecognized one is silently ignored rather than erroring,
        # since the caller isn't waiting for a reply either way.
        return None

    handler = METHODS.get(method)
    if handler is None:
        return _rpc_error(msg_id, -32601, f"Method not found: {method}")
    return handler(msg_id, params)
