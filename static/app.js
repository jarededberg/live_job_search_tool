const form = document.getElementById("search-form");
const resultsEl = document.getElementById("results");
const statusLine = document.getElementById("status-line");
const paginationEl = document.getElementById("pagination");
const footerStatus = document.getElementById("footer-status");
const departmentSelectEl = document.getElementById("department-select");
const departmentTrigger = document.getElementById("department-trigger");
const departmentMenu = document.getElementById("department-menu");
const commitmentSelect = document.getElementById("commitment");
const sortSelect = document.getElementById("sort");
const locationInput = document.getElementById("location-input");
const locationChipsEl = document.getElementById("location-chips");
const locationDropdown = document.getElementById("location-dropdown");
const locationSelectEl = document.getElementById("location-select");
const syntaxHelpBtn = document.getElementById("syntax-help-btn");
const syntaxHelp = document.getElementById("syntax-help");
const resumeInput = document.getElementById("resume-input");
const resumeDropzone = document.getElementById("resume-dropzone");
const resumeStatus = document.getElementById("resume-status");
const authArea = document.getElementById("auth-area");
const saveSearchBtn = document.getElementById("save-search-btn");
const clearSearchBtn = document.getElementById("clear-search-btn");
const modalOverlay = document.getElementById("modal-overlay");
const modalBox = document.getElementById("modal-box");
const modalContent = document.getElementById("modal-content");
const modalCloseBtn = document.getElementById("modal-close");

let currentPage = 1;
// Populated once /api/facets returns salary_bounds/yoe_bounds and the
// sliders are initialized (see initRangeSliders). null until then, and
// search() just skips sending the corresponding filter params in that
// window (matches how department/commitment selects behave before
// loadFacets() resolves -- no special-casing needed).
let salaryRange = null; // { lo, hi, min, max }
let yoeRange = null; // { lo, hi, min, max }
// Each entry is { type: "text", value: "San Francisco, CA" } for a plain
// scraped-location substring, or { type: "group", value: "remote_us",
// label: "Remote (US)" } for a canonical group chip (see location_groups.py)
let selectedLocations = [];
// Department is multi-select (a real ATS "department" facet is wonky
// enough -- inconsistent naming, occasional junk cohort-tag values, see
// db.py's _JUNK_DEPARTMENT_RE -- that limiting a candidate to picking just
// one was a real complaint). Plain array of the exact department strings
// as returned by /api/facets, checked against by the custom dropdown menu
// below (a native <select multiple> was ruled out -- it renders as an
// always-expanded listbox rather than a closed dropdown, and requires
// ctrl/cmd-click that most people don't know about).
let selectedDepartments = [];
let departmentOptions = []; // raw list from /api/facets, shared by the main-page menu and the wizard's department step
let logoCache = {};
let logoCacheLoaded = false;
let locationGroupList = []; // [{key, label}] fetched from /api/location-groups
let locationGroupLabels = {};

async function loadLocationGroups() {
  try {
    const res = await fetch("/api/location-groups");
    const data = await res.json();
    locationGroupList = data.groups || [];
    locationGroupList.forEach((g) => { locationGroupLabels[g.key] = g.label; });
  } catch (e) {
    locationGroupList = []; // quick-select groups are a nice-to-have; fail quietly
  }
}

// Bare-city recognition for Hunter (hunterParseMessage() below) -- "san
// francisco" alone, no state, should resolve to "San Francisco, CA" the
// same way a resume upload already does server-side (see
// metro_areas.py). metroCityMap is city-name-lowercased -> state abbr;
// metroCityRe is one big alternation of every known city name, longest
// first so "san francisco" matches whole rather than partially. Both
// built once the fetch resolves rather than hand-maintained in JS, so
// this list can never drift out of sync with metro_areas.py.
let metroCityMap = {};
let metroCityRe = null;

async function loadMetroCities() {
  try {
    const res = await fetch("/api/metro-cities");
    const data = await res.json();
    const cities = data.cities || [];
    cities.forEach((c) => { metroCityMap[c.city.toLowerCase()] = c.state; });
    const names = cities
      .map((c) => c.city)
      .sort((a, b) => b.length - a.length) // longest first, e.g. "san francisco" before "san"
      .map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    if (names.length) {
      metroCityRe = new RegExp(`\\b(${names.join("|")})\\b`, "gi");
    }
  } catch (e) {
    metroCityMap = {}; // bare-city recognition is a nice-to-have; fail quietly, "City, ST" still works
    metroCityRe = null;
  }
}

// Common short nicknames/abbreviations people actually type in chat --
// "sf", "nyc", "la", "dc" -- that the metro-city list above can't catch
// because those aren't the city's real name, they're a colloquial
// stand-in for it. Kept as a small hardcoded map (not server-fetched,
// unlike metroCityMap) since this is closer to slang than data -- it
// doesn't change when metro_areas.py changes. Deliberately short and
// conservative: every entry here is unambiguous in a job-search sentence
// (no real English word collides with "nyc"/"philly"/"atl"), unlike
// something like "sea" for Seattle, which was left out on purpose.
const CITY_ABBR_LABELS = {
  "sf": "San Francisco, CA",
  "sfo": "San Francisco, CA",
  "nyc": "New York, NY",
  "la": "Los Angeles, CA",
  "dc": "Washington, DC",
  "philly": "Philadelphia, PA",
  "atl": "Atlanta, GA",
  "vegas": "Las Vegas, NV",
  "nola": "New Orleans, LA",
};
const CITY_ABBR_RE = new RegExp(`\\b(${Object.keys(CITY_ABBR_LABELS).join("|")})\\b`, "gi");

async function loadLogoCache() {
  try {
    const res = await fetch("/logo_cache.json");
    logoCache = await res.json();
  } catch (e) {
    logoCache = {}; // logos are cosmetic; fail quietly and just skip them
  }
  logoCacheLoaded = true; // set regardless of success -- don't keep retrying every search
}

function logoUrlFor(company) {
  const domain = logoCache[company];
  return domain ? `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=64` : null;
}

function qs(params) {
  return Object.entries(params)
    .filter(([, v]) => v !== "" && v !== null && v !== undefined)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");
}

// ---- department multi-select ----

function departmentTriggerLabel() {
  if (!selectedDepartments.length) return "Any department";
  if (selectedDepartments.length === 1) return selectedDepartments[0];
  return `${selectedDepartments.length} departments`;
}

function renderDepartmentMenu() {
  departmentTrigger.textContent = departmentTriggerLabel();
  departmentTrigger.classList.toggle("multi-select-trigger-active", selectedDepartments.length > 0);
  if (!departmentOptions.length) {
    departmentMenu.innerHTML = `<div class="multi-select-empty">No department data yet</div>`;
    return;
  }
  departmentMenu.innerHTML = departmentOptions
    .map((dept) => {
      const checked = selectedDepartments.includes(dept) ? "checked" : "";
      return `<label class="multi-select-option">
        <input type="checkbox" value="${escapeAttr(dept)}" ${checked} />
        <span>${escapeHtml(dept)}</span>
      </label>`;
    })
    .join("");
  departmentMenu.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) {
        if (!selectedDepartments.includes(cb.value)) selectedDepartments.push(cb.value);
      } else {
        selectedDepartments = selectedDepartments.filter((d) => d !== cb.value);
      }
      departmentTrigger.textContent = departmentTriggerLabel();
      departmentTrigger.classList.toggle("multi-select-trigger-active", selectedDepartments.length > 0);
    });
  });
}

departmentTrigger.addEventListener("click", () => {
  departmentMenu.classList.toggle("hidden");
});

document.addEventListener("click", (e) => {
  if (!departmentSelectEl.contains(e.target)) departmentMenu.classList.add("hidden");
});

async function search(page = 1) {
  currentPage = page;
  const q = document.getElementById("q").value.trim();
  const days = document.getElementById("days").value;
  const commitment = commitmentSelect.value;
  const sort = sortSelect.value;

  statusLine.textContent = "Searching…";
  resultsEl.innerHTML = "";
  paginationEl.innerHTML = "";

  const params = new URLSearchParams();
  qs({ q, days, commitment, sort, page, per_page: 50 })
    .split("&")
    .filter(Boolean)
    .forEach((pair) => {
      const [k, v] = pair.split("=");
      params.append(decodeURIComponent(k), decodeURIComponent(v));
    });
  selectedDepartments.forEach((d) => params.append("department", d));
  selectedLocations.forEach((loc) => {
    params.append(loc.type === "group" ? "location_group" : "location", loc.value);
  });
  // Only sent once a handle has actually moved off the slider's own
  // endpoints -- e.g. dragging the min handle up sends salary_min but
  // leaves salary_max unsent (no ceiling), matching db.py's treatment of
  // an unsent bound as "no filter on that side."
  if (salaryRange) {
    if (salaryRange.lo > salaryRange.min) params.append("salary_min", salaryRange.lo);
    if (salaryRange.hi < salaryRange.max) params.append("salary_max", salaryRange.hi);
  }
  if (yoeRange) {
    if (yoeRange.lo > yoeRange.min) params.append("yoe_min", yoeRange.lo);
    if (yoeRange.hi < yoeRange.max) params.append("yoe_max", yoeRange.hi);
  }
  if (hasResume) {
    // Sent on every request once a resume is loaded, not just when sorting
    // by match — match_tier badges are computed server-side (see db.py's
    // _match_info) and shown on cards regardless of sort order, so the
    // server needs these terms whenever a resume is active.
    resumeTitleTerms.forEach((t) => params.append("resume_title_term", t));
    resumeSkillTerms.forEach((t) => params.append("resume_skill_term", t));
    // Only sent once we successfully parsed a US city off the resume --
    // this is what lets db.py demote clearly-non-US postings (Prague,
    // Remote-Canada, etc.) instead of calling them a "best match" just
    // because the title lines up. See location_groups.is_clearly_non_us.
    if (resumeUsBased) params.append("resume_us_based", "1");
    resumeMetroTerms.forEach((t) => params.append("resume_metro_term", t));
  }

  try {
    // Kick the jobs fetch off immediately, and — the first time only —
    // the logo cache fetch alongside it, rather than one after the other.
    // This used to be sequential at page load (fetch all ~3,500 companies'
    // logo domains, THEN start the jobs search), which added the full
    // logo-fetch round trip on top of the search's own latency before
    // anything ever rendered. Running them concurrently caps the wait at
    // whichever one is slower instead of the sum of both. Once the cache
    // is loaded, later searches (paging, filters) skip this entirely.
    const jobsFetch = fetch(`/api/jobs?${params.toString()}`);
    const logoWait = logoCacheLoaded ? Promise.resolve() : loadLogoCache();
    const [res] = await Promise.all([jobsFetch, logoWait]);
    const data = await res.json();
    if (!res.ok) {
      statusLine.textContent = data.error || "Something went wrong with that search.";
      return;
    }
    renderResults(data);
  } catch (e) {
    statusLine.textContent = "Something went wrong searching. Try again in a moment.";
  }
}

function renderResults(data) {
  if (!data.jobs.length) {
    resultsEl.innerHTML = `<div class="empty-state">No matching roles found. Try broader keywords, remove a filter, or double-check your search syntax.</div>`;
    statusLine.textContent = `0 results`;
    paginationEl.innerHTML = "";
    return;
  }

  const start = (data.page - 1) * data.per_page + 1;
  const end = start + data.jobs.length - 1;
  const narrowHint = data.total > 500 ? " — try narrowing your search for more precise results" : "";
  statusLine.textContent =
    `Showing ${start.toLocaleString()}–${end.toLocaleString()} of ${data.total.toLocaleString()} match${data.total === 1 ? "" : "es"}${narrowHint}`;

  resultsEl.innerHTML = data.jobs.map(jobCard).join("");

  const prevDisabled = data.page <= 1 ? "disabled" : "";
  const nextDisabled = data.page >= data.pages ? "disabled" : "";
  const numbersHtml = pageNumbers(data.page, data.pages)
    .map((n) =>
      n === "…"
        ? `<span class="page-ellipsis">…</span>`
        : `<button class="page-num${n === data.page ? " active" : ""}" ${n === data.page ? "disabled" : ""} onclick="search(${n})">${n}</button>`
    )
    .join("");
  // A jump-to-page dropdown alongside the arrows/numbers -- handy once
  // there are more pages than the windowed number list shows at once
  // (see pageNumbers()'s "…" collapsing) and someone wants page 40
  // without clicking Next 39 times.
  const jumpOptionsHtml = Array.from({ length: data.pages }, (_, i) => i + 1)
    .map((n) => `<option value="${n}" ${n === data.page ? "selected" : ""}>Page ${n}</option>`)
    .join("");
  const jumpHtml = data.pages > 1
    ? `<select class="page-jump" aria-label="Jump to page" onchange="search(Number(this.value))">${jumpOptionsHtml}</select>`
    : "";
  paginationEl.innerHTML = `
    <button ${prevDisabled} onclick="search(${data.page - 1})">← Prev</button>
    <div class="page-numbers">${numbersHtml}</div>
    <button ${nextDisabled} onclick="search(${data.page + 1})">Next →</button>
    ${jumpHtml}
  `;
}

// Classic "windowed" page-number list: always show page 1 and the last
// page, plus a small window around the current page (2 on each side),
// collapsing any gap into a single "…". Caps out at ~9 visible number
// buttons even when there are hundreds of pages, rather than either
// rendering every page number (unusable past a few dozen pages) or only
// ever showing Prev/Next (the thing this replaces).
function pageNumbers(current, total) {
  if (total <= 1) return [1];
  const delta = 2;
  const pages = [];
  for (let i = 1; i <= total; i++) {
    if (i === 1 || i === total || (i >= current - delta && i <= current + delta)) {
      pages.push(i);
    }
  }
  const withGaps = [];
  let prev = null;
  for (const p of pages) {
    if (prev !== null) {
      // A gap of exactly one skipped page ("1 2 3 [skip 4] 5") shows the
      // number itself instead of "…" -- collapsing a single page into an
      // ellipsis doesn't save any visual space and just looks like a
      // missing button.
      if (p - prev === 2) withGaps.push(prev + 1);
      else if (p - prev > 2) withGaps.push("…");
    }
    withGaps.push(p);
    prev = p;
  }
  return withGaps;
}

function formatSalary(job) {
  if (!job.salary_min) return "";
  const fmt = (n) => (n % 1000 === 0 ? `${n / 1000}k` : `${(n / 1000).toFixed(0)}k`);
  const text = job.salary_min === job.salary_max
    ? `~$${fmt(job.salary_min)}`
    : `~$${fmt(job.salary_min)}–$${fmt(job.salary_max)}`;
  return text;
}

function logoHtml(company) {
  const url = logoUrlFor(company);
  const initial = escapeHtml((company || "?").trim().charAt(0).toUpperCase() || "?");
  if (!url) {
    return `<div class="job-logo job-logo-fallback">${initial}</div>`;
  }
  // onerror: if the guessed domain's favicon 404s at render time (rare —
  // already filtered during the offline logo_cache.json build, but domains
  // can go dark), fall back to the same lettered placeholder instead of a
  // broken image icon.
  return `<div class="job-logo-wrap"><img class="job-logo" src="${escapeAttr(url)}" alt=""
    onerror="this.outerHTML='<div class=&quot;job-logo job-logo-fallback&quot;>${initial}</div>'" /></div>`;
}

// ---- resume match tiers ----
// Computed server-side now (db.py's _match_info), against title + blurb +
// department, across the FULL result set before pagination — not just a
// client-side scan of the fraction of terms present in title+blurb on
// whatever page happens to be visible. Still a keyword-overlap heuristic,
// not a deep semantic match (labeled as such via the badge tooltip). No
// resume uploaded yet = no badges at all, rather than a meaningless
// default; that's still decided client-side (`hasResume`), the tier itself
// comes straight from `job.match_tier` in the API response.
let resumeTitleTerms = [];
let resumeSkillTerms = [];
let hasResume = false;
// True only when parse-resume successfully matched a US city off the
// resume text (data.matched_city truthy). Gates the resume_us_based=1
// query param so db.py's is_clearly_non_us() demotion only kicks in when
// we're actually confident the candidate is US-based -- see search().
let resumeUsBased = false;
// The candidate's home-metro "City, ST" list (data.location_terms --
// Phoenix -> ["Phoenix, AZ", "Scottsdale, AZ", ...]), sent as
// resume_metro_term params so db.py's _match_info can demote onsite jobs
// that are technically US-based but nowhere near the candidate (Denver,
// Salt Lake City, etc. for a Phoenix resume) instead of badging them
// "best match" purely on title overlap. See the location-proximity fix
// in db.py.
let resumeMetroTerms = [];

function matchBadgeHtml(job) {
  if (!hasResume || !job.match_tier) return "";
  const labels = { best: "Best match", good: "Good match", poor: "Poor match" };
  return `<span class="match-badge match-${job.match_tier}" title="Keyword overlap with your resume's extracted terms — a rough signal, not a full analysis">${labels[job.match_tier]}</span>`;
}

function jobCard(job) {
  const posted = job.posted ? job.posted.slice(0, 10) : "date unknown";
  const location = job.location || "Location not listed";
  const tags = [`<span class="tag tag-source">${escapeHtml(job.source)}</span>`];
  if (job.department) tags.push(`<span class="tag tag-department">${escapeHtml(job.department)}</span>`);
  if (job.commitment) tags.push(`<span class="tag tag-commitment">${escapeHtml(job.commitment)}</span>`);
  const salaryText = formatSalary(job);
  if (salaryText) {
    tags.push(
      `<span class="tag tag-salary" title="Best-effort estimate pulled from the listing, not a guaranteed figure">${escapeHtml(salaryText)}</span>`
    );
  }
  const badge = matchBadgeHtml(job);

  // Years-of-experience badge + qualifications blurb, shown together like a
  // single line of "here's what this role actually wants" — the YOE number
  // pulled out as its own small pill (extract_years_experience in
  // blurb_extractor.py) rather than buried in the sentence, so it reads at
  // a glance instead of requiring the user to parse it out of prose.
  const blurbLine = job.blurb || job.years_experience
    ? `<div class="job-blurb">${job.years_experience ? `<span class="yoe-badge">${escapeHtml(job.years_experience)} YOE</span> ` : ""}${escapeHtml(job.blurb || "")}</div>`
    : "";

  // Tools/tech row: specific product names mentioned in the posting
  // (tools_extractor.py), not a generic skills summary — a company that
  // never mentions a tool by name just doesn't get a row, rather than a
  // guessed one.
  const toolsLine = job.tools && job.tools.length
    ? `<div class="job-tools"><span class="tools-icon" aria-hidden="true">🔧</span>${job.tools.map((t) => `<span class="tool-chip">${escapeHtml(t)}</span>`).join("")}</div>`
    : "";

  // Only rendered once we actually know whether the user's logged in
  // (checkAuth() runs on page load) -- logged-out visitors just don't see
  // an applied toggle at all rather than one that errors on click.
  const appliedBtn = currentUser
    ? `<button type="button" class="applied-toggle${job.applied ? " is-applied" : ""}"
         onclick="toggleApplied('${escapeAttr(job.url)}', this)">${job.applied ? "✓ Applied" : "Mark applied"}</button>`
    : "";

  // This site's own page for the job (see /jobs/<id>-<slug> in app.py) --
  // a real internal link, not just the sitemap, so search engines that
  // have already crawled this results grid can find each job's own page
  // through actual site navigation, not only a sitemap entry. Deliberately
  // NOT replacing the title's existing external apply link above -- that's
  // the one-click "go apply" path people are already used to, and changing
  // its target would add friction to the site's actual core action. This
  // is a second, low-key link instead.
  const detailLink = job.detail_path
    ? `<a class="job-detail-link" href="${escapeAttr(job.detail_path)}">Details</a>`
    : "";

  // Trust signal: when our own scraper last saw this exact listing still
  // live on the company's career page -- distinct from "posted", which is
  // whatever date the employer's ATS reports and never changes even if
  // the listing's gone stale. Only shown when last_seen parses to
  // something sane; a job missing/malformed last_seen (shouldn't happen,
  // but this is third-party-adjacent scraped data) just quietly omits the
  // badge rather than showing "NaN ago".
  const lastSeenAgo = timeAgo(job.last_seen);
  const freshnessBadge = lastSeenAgo
    ? `<span class="tag tag-freshness" title="Our scraper last confirmed this listing was still live on the company's own career page ${escapeAttr(lastSeenAgo)}">✓ Confirmed ${escapeHtml(lastSeenAgo)}</span>`
    : "";

  return `
    <div class="job-card">
      <div class="job-card-header">
        ${logoHtml(job.company)}
        <div class="job-header-text">
          <div class="job-title"><a href="${escapeAttr(job.url)}" target="_blank" rel="noopener">${escapeHtml(job.title)}</a></div>
          <div class="job-sub">${escapeHtml(job.company)} · ${escapeHtml(location)}</div>
        </div>
      </div>
      ${badge}
      ${blurbLine}
      ${toolsLine}
      <div class="job-footer">
        <span class="job-posted">${escapeHtml(posted)}</span>
        ${tags.join("")}
        ${freshnessBadge}
        ${detailLink}
        ${appliedBtn}
      </div>
    </div>
  `;
}

// "X ago" trust-signal phrasing, shared by job cards, the job detail page
// (see app.py's matching _time_ago() -- kept as two separate small
// functions rather than one shared file since the detail page is
// server-rendered Python and job cards are client-rendered JS, but both
// need to agree on the same phrasing/thresholds so a listing doesn't
// read as "confirmed 2 hours ago" on the card and "confirmed today" on
// its own detail page) and the homepage footer's dataset-refreshed line.
// Deliberately coarse buckets (minutes/hours/days/weeks), not exact
// seconds -- this is a trust signal ("we checked recently"), not a
// precise audit log.
function timeAgo(isoString) {
  if (!isoString) return "";
  const then = new Date(isoString.endsWith("Z") || isoString.includes("+") ? isoString : isoString + "Z");
  const diffMs = Date.now() - then.getTime();
  if (isNaN(diffMs)) return "";
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} day${days === 1 ? "" : "s"} ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 5) return `${weeks} week${weeks === 1 ? "" : "s"} ago`;
  const months = Math.floor(days / 30);
  return `${months} month${months === 1 ? "" : "s"} ago`;
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    if (data.scrape_in_progress) {
      const p = data.scrape_progress;
      footerStatus.textContent = p
        ? `Refreshing dataset now… ${p.done.toLocaleString()} / ${p.total.toLocaleString()} companies checked.`
        : "Refreshing dataset now…";
    } else if (data.last_run) {
      // Relative "X ago" phrasing (see timeAgo()) rather than a raw
      // locale timestamp -- "last refreshed 12 minutes ago" reads as an
      // active, trustworthy dataset at a glance; a bare timestamp makes
      // the visitor do the subtraction themselves to get the same signal.
      const ago = timeAgo(data.last_run.finished_at) || "recently";
      footerStatus.textContent =
        `${data.total_jobs.toLocaleString()} open roles cached from ${data.total_companies.toLocaleString()} companies · ` +
        `last refreshed ${ago}`;
    } else {
      footerStatus.textContent = "First scrape hasn't finished yet — check back shortly.";
    }
  } catch (e) {
    footerStatus.textContent = "";
  }
}

async function loadFacets() {
  try {
    const res = await fetch("/api/facets");
    const data = await res.json();
    departmentOptions = data.departments || [];
    renderDepartmentMenu();
    fillSelect(commitmentSelect, data.commitments, "Any commitment");
    if (data.salary_bounds && data.yoe_bounds) {
      initRangeSliders(data.salary_bounds, data.yoe_bounds);
    }
  } catch (e) {
    /* facets are a nice-to-have; fail quietly */
  }
}

// ---- salary / YOE dual-range sliders ----
//
// Plain custom sliders (pointer-events based, not two overlapping native
// <input type="range"> elements) -- overlapping range inputs have real
// quirks around which thumb receives a click when they're stacked at the
// same position, and this app already leans on vanilla JS everywhere
// else rather than pulling in a slider library for two fields.

function createRangeSlider({ track, fill, thumbMin, thumbMax, valueEl, min, max, step, format, onCommit }) {
  let lo = min;
  let hi = max;

  function pct(v) {
    return max === min ? 0 : ((v - min) / (max - min)) * 100;
  }

  function render() {
    thumbMin.style.left = `${pct(lo)}%`;
    thumbMax.style.left = `${pct(hi)}%`;
    fill.style.left = `${pct(lo)}%`;
    fill.style.width = `${Math.max(0, pct(hi) - pct(lo))}%`;
    thumbMin.setAttribute("aria-valuemin", min);
    thumbMin.setAttribute("aria-valuemax", max);
    thumbMin.setAttribute("aria-valuenow", lo);
    thumbMax.setAttribute("aria-valuemin", min);
    thumbMax.setAttribute("aria-valuemax", max);
    thumbMax.setAttribute("aria-valuenow", hi);
    // "Any" when untouched, "$150k+" when only the floor moved (no
    // ceiling requested), "Up to $150k" when only the ceiling moved, and
    // a plain "$X – $Y" once both handles are off their endpoints.
    if (lo === min && hi === max) valueEl.textContent = "Any";
    else if (hi === max) valueEl.textContent = `${format(lo)}+`;
    else if (lo === min) valueEl.textContent = `Up to ${format(hi)}`;
    else valueEl.textContent = `${format(lo)} – ${format(hi)}`;
  }

  function valueFromClientX(clientX) {
    const rect = track.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    const raw = min + ratio * (max - min);
    return Math.round(raw / step) * step;
  }

  function startDrag(which, thumb, e) {
    e.preventDefault();
    thumb.setPointerCapture(e.pointerId);
    thumb.classList.add("dragging");
    function move(ev) {
      const v = valueFromClientX(ev.clientX);
      if (which === "min") lo = Math.min(v, hi);
      else hi = Math.max(v, lo);
      render();
    }
    function up() {
      thumb.classList.remove("dragging");
      thumb.removeEventListener("pointermove", move);
      thumb.removeEventListener("pointerup", up);
      onCommit(lo, hi);
    }
    thumb.addEventListener("pointermove", move);
    thumb.addEventListener("pointerup", up);
  }

  thumbMin.addEventListener("pointerdown", (e) => startDrag("min", thumbMin, e));
  thumbMax.addEventListener("pointerdown", (e) => startDrag("max", thumbMax, e));

  // Arrow-key nudging, same step as dragging -- keeps the sliders usable
  // without a mouse/touchscreen (native <input type="range"> gets this
  // for free; a custom div-based slider has to implement it by hand).
  function onKey(which, e) {
    let delta = 0;
    if (e.key === "ArrowRight" || e.key === "ArrowUp") delta = step;
    else if (e.key === "ArrowLeft" || e.key === "ArrowDown") delta = -step;
    else if (e.key === "Home") delta = which === "min" ? min - lo : min - hi;
    else if (e.key === "End") delta = which === "min" ? max - lo : max - hi;
    else return;
    e.preventDefault();
    if (which === "min") lo = Math.min(Math.max(min, lo + delta), hi);
    else hi = Math.max(Math.min(max, hi + delta), lo);
    render();
    onCommit(lo, hi);
  }
  thumbMin.addEventListener("keydown", (e) => onKey("min", e));
  thumbMax.addEventListener("keydown", (e) => onKey("max", e));

  // Clicking the bare track (not a thumb) jumps whichever handle is
  // closer to the click point -- standard dual-slider behavior.
  track.addEventListener("click", (e) => {
    if (e.target !== track) return;
    const v = valueFromClientX(e.clientX);
    if (Math.abs(v - lo) <= Math.abs(v - hi)) lo = Math.min(v, hi);
    else hi = Math.max(v, lo);
    render();
    onCommit(lo, hi);
  });

  render();
  return {
    getValues: () => ({ lo, hi, min, max }),
    // Used to restore slider position when loading a saved search --
    // clamps into [min, max] and re-renders the thumbs/fill/label without
    // re-running onCommit (the caller triggers the actual search itself).
    setValues(newLo, newHi) {
      lo = Math.min(Math.max(min, newLo), max);
      hi = Math.min(Math.max(min, newHi), max);
      if (lo > hi) [lo, hi] = [hi, lo];
      render();
    },
  };
}

function formatSalaryShort(n) {
  return `$${Math.round(n / 1000)}k`;
}

function formatYoeShort(n) {
  return `${n} yr${n === 1 ? "" : "s"}`;
}

let rangeSlidersInitialized = false;
// Kept around (beyond the closures createRangeSlider already returns) so
// a loaded saved search can reposition the thumbs via .setValues() --
// see loadSavedSearch().
let salarySliderCtl = null;
let yoeSliderCtl = null;

function initRangeSliders(salaryBounds, yoeBounds) {
  if (rangeSlidersInitialized) return; // /api/facets can be called again later; only wire once
  rangeSlidersInitialized = true;

  const salaryStep = Math.max(1000, Math.round((salaryBounds.max - salaryBounds.min) / 100 / 1000) * 1000);
  salarySliderCtl = createRangeSlider({
    track: document.querySelector("#salary-slider .range-slider-track"),
    fill: document.getElementById("salary-slider-fill"),
    thumbMin: document.getElementById("salary-thumb-min"),
    thumbMax: document.getElementById("salary-thumb-max"),
    valueEl: document.getElementById("salary-range-value"),
    min: salaryBounds.min,
    max: salaryBounds.max,
    step: salaryStep,
    format: formatSalaryShort,
    onCommit: (lo, hi) => {
      salaryRange = { lo, hi, min: salaryBounds.min, max: salaryBounds.max };
      search(1);
    },
  });
  salaryRange = { lo: salaryBounds.min, hi: salaryBounds.max, min: salaryBounds.min, max: salaryBounds.max };

  const yoeStep = 1;
  yoeSliderCtl = createRangeSlider({
    track: document.querySelector("#yoe-slider .range-slider-track"),
    fill: document.getElementById("yoe-slider-fill"),
    thumbMin: document.getElementById("yoe-thumb-min"),
    thumbMax: document.getElementById("yoe-thumb-max"),
    valueEl: document.getElementById("yoe-range-value"),
    min: yoeBounds.min,
    max: yoeBounds.max,
    step: yoeStep,
    format: formatYoeShort,
    onCommit: (lo, hi) => {
      yoeRange = { lo, hi, min: yoeBounds.min, max: yoeBounds.max };
      search(1);
    },
  });
  yoeRange = { lo: yoeBounds.min, hi: yoeBounds.max, min: yoeBounds.min, max: yoeBounds.max };
}

function fillSelect(select, values, defaultLabel) {
  const current = select.value;
  select.innerHTML = `<option value="">${defaultLabel}</option>` +
    values.map((v) => `<option value="${escapeAttr(v)}">${escapeHtml(v)}</option>`).join("");
  if (values.includes(current)) select.value = current;
}

// ---- location typeahead (multi-select) ----

let locationDebounce = null;
let locationOptions = [];
let locationActiveIndex = -1;

function renderLocationChips() {
  locationChipsEl.innerHTML = selectedLocations
    .map((loc, i) => {
      const label = loc.label || loc.value;
      const cls = loc.type === "group" ? "location-chip location-chip-group" : "location-chip";
      return `<span class="${cls}">${escapeHtml(label)}<button type="button" data-idx="${i}" aria-label="Remove ${escapeAttr(label)}">×</button></span>`;
    })
    .join("");
  locationChipsEl.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      selectedLocations.splice(Number(btn.dataset.idx), 1);
      renderLocationChips();
      search(1);
    });
  });
}

function hideLocationDropdown() {
  locationDropdown.classList.add("hidden");
  locationDropdown.innerHTML = "";
  locationOptions = [];
  locationActiveIndex = -1;
}

function renderLocationDropdown(options) {
  locationOptions = options;
  locationActiveIndex = -1;
  if (!options.length) {
    locationDropdown.innerHTML = `<div class="location-option-empty">No matching locations</div>`;
  } else {
    locationDropdown.innerHTML = options
      .map((opt, i) => {
        const cls = opt.type === "group" ? "location-option location-option-group" : "location-option";
        return `<div class="${cls}" data-idx="${i}">${escapeHtml(opt.label)}</div>`;
      })
      .join("");
    locationDropdown.querySelectorAll(".location-option").forEach((el) => {
      el.addEventListener("mousedown", (e) => {
        e.preventDefault(); // keep focus on the input so blur doesn't fire first
        pickLocation(options[Number(el.dataset.idx)]);
      });
    });
  }
  locationDropdown.classList.remove("hidden");
}

function pickLocation(option) {
  if (typeof option === "string") option = { type: "text", value: option, label: option };
  const isDup = (opt) => selectedLocations.some((s) => s.type === opt.type && s.value === opt.value);
  if (option && option.value && !isDup(option)) {
    selectedLocations.push(option);
    renderLocationChips();
    search(1);
  }
  locationInput.value = "";
  hideLocationDropdown();
}

async function fetchLocationOptions(query) {
  const q = query.trim().toLowerCase();
  // Canonical groups are pinned quick-select options — shown whenever their
  // label matches what's typed (or always, when the box is empty) — kept
  // separate from raw per-company location strings pulled from the DB below.
  const groupMatches = locationGroupList
    .filter((g) => !q || g.label.toLowerCase().includes(q))
    .map((g) => ({ type: "group", value: g.key, label: g.label }));

  let textMatches = [];
  try {
    const res = await fetch(`/api/locations?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    textMatches = (data.locations || []).map((v) => ({ type: "text", value: v, label: v }));
  } catch (e) {
    /* raw suggestions are a nice-to-have; fail quietly */
  }

  const isSelected = (opt) => selectedLocations.some((s) => s.type === opt.type && s.value === opt.value);
  return [...groupMatches, ...textMatches].filter((opt) => !isSelected(opt));
}

locationInput.addEventListener("input", () => {
  clearTimeout(locationDebounce);
  const query = locationInput.value.trim();
  locationDebounce = setTimeout(async () => {
    const options = await fetchLocationOptions(query);
    renderLocationDropdown(options);
  }, 200);
});

locationInput.addEventListener("focus", () => {
  if (locationInput.value.trim() === "" && locationOptions.length === 0) {
    fetchLocationOptions("").then(renderLocationDropdown);
  }
});

locationInput.addEventListener("keydown", (e) => {
  if (locationDropdown.classList.contains("hidden")) return;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    locationActiveIndex = Math.min(locationActiveIndex + 1, locationOptions.length - 1);
    updateActiveOption();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    locationActiveIndex = Math.max(locationActiveIndex - 1, 0);
    updateActiveOption();
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (locationActiveIndex >= 0 && locationOptions[locationActiveIndex]) {
      pickLocation(locationOptions[locationActiveIndex]);
    } else if (locationInput.value.trim()) {
      pickLocation(locationInput.value.trim());
    }
  } else if (e.key === "Escape") {
    hideLocationDropdown();
  } else if (e.key === "Backspace" && locationInput.value === "" && selectedLocations.length) {
    selectedLocations.pop();
    renderLocationChips();
    search(1);
  }
});

function updateActiveOption() {
  locationDropdown.querySelectorAll(".location-option").forEach((el, i) => {
    el.classList.toggle("active", i === locationActiveIndex);
  });
}

document.addEventListener("click", (e) => {
  if (!locationSelectEl.contains(e.target)) hideLocationDropdown();
});

// ---- resume upload ----

async function handleResumeFile(file) {
  if (!file) return;
  resumeStatus.textContent = "Reading resume…";
  resumeStatus.className = "resume-status";

  const formData = new FormData();
  formData.append("resume", file);

  try {
    const res = await fetch("/api/parse-resume", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      resumeStatus.textContent = data.message || "Couldn't parse that resume.";
      resumeStatus.className = "resume-status error";
      return;
    }
    resumeTitleTerms = (data.title_terms || []).map((t) => t.toLowerCase());
    resumeSkillTerms = (data.skill_terms || []).map((t) => t.toLowerCase());
    hasResume = true;
    resumeUsBased = Boolean(data.matched_city);
    resumeMetroTerms = (data.location_terms || []).map((t) => t.toLowerCase());
    document.getElementById("sort-match-option").hidden = false;
    sortSelect.value = "match";

    // Deliberately NOT auto-filling the query box or auto-adding location
    // chips anymore. Both used to apply automatically, and stacked
    // together they were brutal: a synthetic 600-job test went 600 -> 109
    // (query box narrowing to just the 8 extracted title phrases, matched
    // against title text only) -> 7 (location chips on top, excluding
    // every onsite job outside the resume's home metro). That directly
    // fought the match-tier sort, whose whole point is to rank the FULL
    // dataset by fit so the best matches float to the top without hiding
    // everything else. Now: leave query/location untouched, search
    // immediately with sort=match so all open roles show up ranked by fit,
    // and offer the extracted query + location as one-click opt-in
    // refinements instead of a default filter.
    const suggestions = [];
    if (data.query) {
      suggestions.push(
        `<button type="button" id="apply-resume-query" class="resume-suggestion-btn">Narrow to these titles</button>`
      );
    }
    if ((data.location_terms && data.location_terms.length) || (data.location_groups && data.location_groups.length)) {
      const label = data.matched_city ? `${data.matched_city} area + Remote (US)` : "Remote (US)";
      suggestions.push(
        `<button type="button" id="apply-resume-location" class="resume-suggestion-btn">Narrow to ${escapeHtml(label)}</button>`
      );
    }
    const suggestionHtml = suggestions.length
      ? ` Optional narrowing: ${suggestions.join(" ")}`
      : "";
    resumeStatus.innerHTML =
      `Extracted: ${escapeHtml(data.terms.join(", "))} — sorted by best match across all open roles.${suggestionHtml}`;
    resumeStatus.className = "resume-status ok";

    const applyQueryBtn = document.getElementById("apply-resume-query");
    if (applyQueryBtn) {
      applyQueryBtn.addEventListener("click", () => {
        document.getElementById("q").value = data.query;
        search(1);
      });
    }
    const applyLocationBtn = document.getElementById("apply-resume-location");
    if (applyLocationBtn) {
      applyLocationBtn.addEventListener("click", () => {
        (data.location_terms || []).forEach((loc) => {
          if (!selectedLocations.some((s) => s.type === "text" && s.value === loc)) {
            selectedLocations.push({ type: "text", value: loc, label: loc });
          }
        });
        (data.location_groups || []).forEach((key) => {
          const label = locationGroupLabels[key] || key;
          if (!selectedLocations.some((s) => s.type === "group" && s.value === key)) {
            selectedLocations.push({ type: "group", value: key, label });
          }
        });
        renderLocationChips();
        search(1);
      });
    }

    search(1);
  } catch (e) {
    resumeStatus.textContent = "Upload failed. Try again.";
    resumeStatus.className = "resume-status error";
  }
}

resumeInput.addEventListener("change", (e) => handleResumeFile(e.target.files[0]));

["dragover", "dragenter"].forEach((evt) =>
  resumeDropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    resumeDropzone.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((evt) =>
  resumeDropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    resumeDropzone.classList.remove("dragover");
  })
);
resumeDropzone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (file) handleResumeFile(file);
});

// ---- accounts: modal shell ----

let currentUser = null; // { email } once logged in via checkAuth(), else null
// Set from /api/auth-config on load -- empty string means Turnstile isn't
// configured on this deployment, and the signup form just skips rendering
// the widget entirely (matches the backend's verify_turnstile() skipping
// the check when TURNSTILE_SECRET_KEY is unset). See README.
let turnstileSiteKey = "";
let turnstileWidgetId = null;
// Whether this deployment has RESEND_API_KEY set (see app.py's
// password_reset_enabled()) -- gates whether "Forgot password?" renders on
// the login form at all. A deployment without it configured just doesn't
// show the link, same graceful-degradation pattern as everything else here.
let passwordResetEnabled = false;

async function loadAuthConfig() {
  try {
    const res = await fetch("/api/auth-config");
    const data = await res.json();
    turnstileSiteKey = data.turnstile_site_key || "";
    passwordResetEnabled = Boolean(data.password_reset_enabled);
  } catch (e) {
    turnstileSiteKey = ""; // fail quiet -- same as every other nice-to-have fetch here
    passwordResetEnabled = false;
  }
}

function renderTurnstileWidget() {
  const el = document.getElementById("turnstile-widget");
  if (!el || !turnstileSiteKey) return;
  if (window.turnstile) {
    turnstileWidgetId = window.turnstile.render(el, { sitekey: turnstileSiteKey });
  } else {
    // api.js loads async -- if the signup modal opens before it's ready
    // (rare, only right after initial page load), poll briefly rather
    // than just giving up on the widget.
    setTimeout(renderTurnstileWidget, 150);
  }
}

function openModal(html, { wide = false, auth = false } = {}) {
  modalContent.innerHTML = html;
  modalBox.classList.toggle("modal-wide", wide);
  // The auth flows (login/signup/forgot/reset) get a distinct branded-
  // header treatment (see .modal-auth in style.css) instead of the plain
  // .modal-title look the saved-searches/applied-jobs modals use -- this
  // is the one modal most first-time visitors actually see.
  modalBox.classList.toggle("modal-auth", auth);
  modalOverlay.classList.remove("hidden");
}

// Small inline icons for the auth forms' input fields -- plain stroked SVG
// (matches the site's minimal line-icon aesthetic elsewhere) rather than
// pulling in an icon font/library for two icons.
const ICON_MAIL = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>`;
const ICON_LOCK = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>`;

function closeModal() {
  modalOverlay.classList.add("hidden");
  modalContent.innerHTML = "";
}

modalCloseBtn.addEventListener("click", closeModal);
modalOverlay.addEventListener("click", (e) => {
  if (e.target === modalOverlay) closeModal(); // click on the dimmed backdrop, not the box itself
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !modalOverlay.classList.contains("hidden")) closeModal();
});

// ---- accounts: auth (signup/login/logout) ----

function renderAuthArea() {
  if (currentUser) {
    authArea.innerHTML = `
      <button type="button" class="auth-btn" id="btn-saved-searches">Saved searches</button>
      <button type="button" class="auth-btn" id="btn-my-applications">My applications</button>
      <span class="auth-email">${escapeHtml(currentUser.email)}</span>
      <button type="button" class="auth-btn" id="btn-logout">Log out</button>
    `;
    document.getElementById("btn-saved-searches").addEventListener("click", openSavedSearchesModal);
    document.getElementById("btn-my-applications").addEventListener("click", openMyApplicationsModal);
    document.getElementById("btn-logout").addEventListener("click", logout);
    saveSearchBtn.classList.remove("hidden");
  } else {
    authArea.innerHTML = `
      <button type="button" class="auth-btn" id="btn-login">Log in</button>
      <button type="button" class="auth-btn auth-btn-primary" id="btn-signup">Sign up</button>
    `;
    document.getElementById("btn-login").addEventListener("click", () => openAuthModal("login"));
    document.getElementById("btn-signup").addEventListener("click", () => openAuthModal("signup"));
    saveSearchBtn.classList.add("hidden");
    document.getElementById("save-search-inline").classList.add("hidden");
  }
}

async function checkAuth() {
  try {
    const res = await fetch("/api/me");
    const data = await res.json();
    currentUser = data.ok ? { email: data.email } : null;
  } catch (e) {
    currentUser = null; // accounts DB hiccup or not configured -- fail quiet, site works fine logged-out
  }
  renderAuthArea();
}

async function logout() {
  try {
    await fetch("/api/logout", { method: "POST" });
  } catch (e) { /* best effort -- clear local state regardless */ }
  currentUser = null;
  renderAuthArea();
  search(currentPage); // redraw cards without the "Mark applied" toggle
}

function openAuthModal(mode) {
  const isLogin = mode === "login";
  turnstileWidgetId = null; // previous modal's widget (if any) is gone with its DOM
  // Turnstile only guards signup (where bot account-creation is the actual
  // risk) -- login is left alone, rate limiting on /api/login covers
  // credential-stuffing without adding a captcha to every login attempt.
  const showTurnstile = !isLogin && turnstileSiteKey;
  openModal(`
    <div class="modal-auth-header">
      <div class="modal-auth-brand"><span class="brand-mark">◆</span> Skip The Boards</div>
      <h2>${isLogin ? "Welcome back" : "Create your account"}</h2>
      <p>${isLogin ? "Log in to pick up your saved searches and applications." : "Save searches and track which roles you've applied to."}</p>
    </div>
    <div class="modal-auth-body">
      <form id="auth-form">
        <label for="auth-email">Email</label>
        <div class="input-icon-group">
          ${ICON_MAIL}
          <input type="email" id="auth-email" required autocomplete="email" />
        </div>
        <label for="auth-password">Password</label>
        <div class="input-icon-group">
          ${ICON_LOCK}
          <input type="password" id="auth-password" required minlength="8"
            autocomplete="${isLogin ? "current-password" : "new-password"}" />
        </div>
        ${isLogin && passwordResetEnabled ? `<p class="modal-forgot"><a href="#" id="auth-forgot-link">Forgot password?</a></p>` : ""}
        ${showTurnstile ? `<div id="turnstile-widget"></div>` : ""}
        <div class="auth-error" id="auth-error"></div>
        <button type="submit" class="btn-primary">${isLogin ? "Log in" : "Create account"}</button>
      </form>
      <p class="modal-switch">
        ${isLogin ? "No account?" : "Already have an account?"}
        <a href="#" id="auth-switch">${isLogin ? "Sign up" : "Log in"}</a>
      </p>
    </div>
  `, { auth: true });
  if (showTurnstile) renderTurnstileWidget();
  document.getElementById("auth-switch").addEventListener("click", (e) => {
    e.preventDefault();
    openAuthModal(isLogin ? "signup" : "login");
  });
  const forgotLink = document.getElementById("auth-forgot-link");
  if (forgotLink) {
    forgotLink.addEventListener("click", (e) => {
      e.preventDefault();
      openForgotPasswordModal();
    });
  }
  document.getElementById("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = document.getElementById("auth-email").value.trim();
    const password = document.getElementById("auth-password").value;
    const errorEl = document.getElementById("auth-error");
    errorEl.textContent = "";
    const body = { email, password };
    if (showTurnstile) {
      body.turnstile_token = (turnstileWidgetId !== null && window.turnstile)
        ? window.turnstile.getResponse(turnstileWidgetId)
        : "";
      if (!body.turnstile_token) {
        errorEl.textContent = "Please complete the verification check.";
        return;
      }
    }
    try {
      const res = await fetch(isLogin ? "/api/login" : "/api/signup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        errorEl.textContent = data.message || "Something went wrong.";
        // A rejected/expired Turnstile token needs a fresh challenge, not
        // a resubmit of the same stale one.
        if (showTurnstile && window.turnstile && turnstileWidgetId !== null) {
          window.turnstile.reset(turnstileWidgetId);
        }
        return;
      }
      currentUser = { email: data.email };
      renderAuthArea();
      closeModal();
      search(currentPage); // redraw cards with applied toggles now available
    } catch (err) {
      errorEl.textContent = "Something went wrong. Try again.";
    }
  });
}

// ---- accounts: password reset ----

function openForgotPasswordModal() {
  openModal(`
    <div class="modal-auth-header">
      <div class="modal-auth-brand"><span class="brand-mark">◆</span> Skip The Boards</div>
      <h2>Reset your password</h2>
      <p>Enter your account email and we'll send you a link to set a new one.</p>
    </div>
    <div class="modal-auth-body">
      <form id="forgot-form">
        <label for="forgot-email">Email</label>
        <div class="input-icon-group">
          ${ICON_MAIL}
          <input type="email" id="forgot-email" required autocomplete="email" />
        </div>
        <div class="auth-error" id="forgot-error"></div>
        <div class="auth-success hidden" id="forgot-success"></div>
        <button type="submit" class="btn-primary">Send reset link</button>
      </form>
      <p class="modal-switch">
        Remembered it? <a href="#" id="forgot-back-link">Log in</a>
      </p>
    </div>
  `, { auth: true });
  document.getElementById("forgot-back-link").addEventListener("click", (e) => {
    e.preventDefault();
    openAuthModal("login");
  });
  document.getElementById("forgot-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const email = document.getElementById("forgot-email").value.trim();
    const errorEl = document.getElementById("forgot-error");
    const successEl = document.getElementById("forgot-success");
    const submitBtn = form.querySelector("button[type=submit]");
    errorEl.textContent = "";
    submitBtn.disabled = true;
    try {
      const res = await fetch("/api/forgot-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const data = await res.json();
      // Shown regardless of whether the email actually had an account --
      // the backend's response is deliberately the same either way (see
      // api_forgot_password in app.py), so this UI can't leak that either.
      // Only a genuine request failure (network error, 429 rate limit)
      // shows something different, via the catch block below.
      successEl.textContent = data.message || "If that email has an account, a reset link is on its way.";
      successEl.classList.remove("hidden");
      document.getElementById("forgot-email").disabled = true;
      submitBtn.textContent = "Sent";
    } catch (err) {
      errorEl.textContent = "Something went wrong. Try again.";
      submitBtn.disabled = false;
    }
  });
}

function openResetPasswordModal(token) {
  openModal(`
    <div class="modal-auth-header">
      <div class="modal-auth-brand"><span class="brand-mark">◆</span> Skip The Boards</div>
      <h2>Set a new password</h2>
      <p>Choose a new password for your account.</p>
    </div>
    <div class="modal-auth-body">
      <form id="reset-password-form">
        <label for="reset-password-input">New password</label>
        <div class="input-icon-group">
          ${ICON_LOCK}
          <input type="password" id="reset-password-input" required minlength="8" autocomplete="new-password" />
        </div>
        <div class="auth-error" id="reset-password-error"></div>
        <button type="submit" class="btn-primary">Update password</button>
      </form>
    </div>
  `, { auth: true });
  document.getElementById("reset-password-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const password = document.getElementById("reset-password-input").value;
    const errorEl = document.getElementById("reset-password-error");
    errorEl.textContent = "";
    try {
      const res = await fetch("/api/reset-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, password }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        errorEl.textContent = data.message || "Something went wrong.";
        return;
      }
      // Strip the token out of the URL on success -- a used token is dead
      // either way (see db_users.consume_reset_token), but leaving it
      // sitting in the address bar/history/a refresh is just untidy at
      // best and confusing at worst ("why does this form still say update
      // password after I already did").
      const cleanPath = window.location.pathname === "/reset-password" ? "/" : window.location.pathname;
      history.replaceState({}, "", cleanPath);
      closeModal();
      statusLine.textContent = "Password updated -- log in with your new password.";
      openAuthModal("login");
    } catch (err) {
      errorEl.textContent = "Something went wrong. Try again.";
    }
  });
}

// If the page loaded from the link in a reset email (/reset-password?token=...),
// jump straight to the "set a new password" modal instead of making the
// user hunt for a login button first.
function checkForResetToken() {
  if (window.location.pathname !== "/reset-password") return;
  const token = new URLSearchParams(window.location.search).get("token");
  if (token) openResetPasswordModal(token);
}

// ---- accounts: saved searches ----

function currentSearchParams() {
  // The "restorable" subset of search state -- plain filters, location
  // chips, and (if actually touched) the salary/YOE sliders. Deliberately
  // excludes anything resume-derived (resume_title_term etc.): a saved
  // search is meant to be re-run standalone later, without requiring the
  // same resume to be re-uploaded first, so "match" sort (which only
  // means something with resume terms attached) falls back to "newest."
  const params = {
    q: document.getElementById("q").value.trim(),
    days: document.getElementById("days").value,
    departments: selectedDepartments.slice(),
    commitment: commitmentSelect.value,
    sort: sortSelect.value === "match" ? "newest" : sortSelect.value,
    locations: selectedLocations,
  };
  if (salaryRange && (salaryRange.lo > salaryRange.min || salaryRange.hi < salaryRange.max)) {
    params.salary_min = salaryRange.lo;
    params.salary_max = salaryRange.hi;
  }
  if (yoeRange && (yoeRange.lo > yoeRange.min || yoeRange.hi < yoeRange.max)) {
    params.yoe_min = yoeRange.lo;
    params.yoe_max = yoeRange.hi;
  }
  return params;
}

function loadSavedSearch(params) {
  document.getElementById("q").value = params.q || "";
  document.getElementById("days").value = params.days || "";
  // `departments` (array) is the current shape -- `department` (singular
  // string) is kept for backward compatibility with searches saved before
  // department became multi-select.
  selectedDepartments = params.departments || (params.department ? [params.department] : []);
  renderDepartmentMenu();
  if (params.commitment) commitmentSelect.value = params.commitment;
  sortSelect.value = params.sort || "newest";
  selectedLocations = params.locations || [];
  renderLocationChips();
  if (salarySliderCtl && params.salary_min !== undefined) {
    salaryRange = { ...salaryRange, lo: params.salary_min, hi: params.salary_max };
    salarySliderCtl.setValues(params.salary_min, params.salary_max);
  }
  if (yoeSliderCtl && params.yoe_min !== undefined) {
    yoeRange = { ...yoeRange, lo: params.yoe_min, hi: params.yoe_max };
    yoeSliderCtl.setValues(params.yoe_min, params.yoe_max);
  }
  closeModal();
  search(1);
}

saveSearchBtn.addEventListener("click", () => {
  saveSearchBtn.classList.add("hidden");
  const inline = document.getElementById("save-search-inline");
  inline.classList.remove("hidden");
  document.getElementById("save-search-name").focus();
});

document.getElementById("save-search-cancel").addEventListener("click", () => {
  document.getElementById("save-search-inline").classList.add("hidden");
  document.getElementById("save-search-name").value = "";
  saveSearchBtn.classList.remove("hidden");
});

document.getElementById("save-search-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); submitSaveSearch(); }
  else if (e.key === "Escape") { document.getElementById("save-search-cancel").click(); }
});

document.getElementById("save-search-confirm").addEventListener("click", submitSaveSearch);

async function submitSaveSearch() {
  const nameInput = document.getElementById("save-search-name");
  const name = nameInput.value.trim();
  if (!name) { nameInput.focus(); return; }
  try {
    const res = await fetch("/api/saved-searches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, params: currentSearchParams() }),
    });
    const data = await res.json();
    statusLine.textContent = (res.ok && data.ok) ? `Saved "${name}".` : (data.message || "Couldn't save that search.");
  } catch (e) {
    statusLine.textContent = "Couldn't save that search. Try again.";
  } finally {
    nameInput.value = "";
    document.getElementById("save-search-inline").classList.add("hidden");
    saveSearchBtn.classList.remove("hidden");
  }
}

async function openSavedSearchesModal() {
  openModal(`<h2 class="modal-title">Saved searches</h2><div id="saved-searches-list">Loading…</div>`);
  try {
    const res = await fetch("/api/saved-searches");
    const data = await res.json();
    const list = document.getElementById("saved-searches-list");
    if (!data.searches || !data.searches.length) {
      list.innerHTML = `<div class="empty-modal-state">No saved searches yet. Run a search, then click "Save this search."</div>`;
      return;
    }
    list.innerHTML = data.searches.map((s) => `
      <div class="saved-search-row">
        <span class="saved-search-name">${escapeHtml(s.name)}</span>
        <label class="saved-search-alert-toggle" title="Email me when new jobs match this search">
          <input type="checkbox" data-alert-toggle="${s.id}" ${s.alerts_enabled ? "checked" : ""} />
          Email alerts
        </label>
        <select class="saved-search-frequency" data-frequency-select="${s.id}"
          title="How often to check for new matches" ${s.alerts_enabled ? "" : "disabled"}>
          <option value="daily" ${s.alert_frequency === "daily" ? "selected" : ""}>Daily</option>
          <option value="weekly" ${s.alert_frequency === "weekly" ? "selected" : ""}>Weekly</option>
        </select>
        <button type="button" class="row-action-btn" data-run="${s.id}">Run</button>
        <button type="button" class="row-action-btn danger" data-delete="${s.id}">Delete</button>
      </div>
    `).join("");
    const byId = {};
    data.searches.forEach((s) => { byId[s.id] = s; });
    list.querySelectorAll("[data-run]").forEach((btn) => {
      btn.addEventListener("click", () => loadSavedSearch(byId[btn.dataset.run].params));
    });
    list.querySelectorAll("[data-delete]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await fetch(`/api/saved-searches/${btn.dataset.delete}`, { method: "DELETE" });
        openSavedSearchesModal(); // simplest correct redraw -- just refetch rather than hand-patch the DOM
      });
    });
    list.querySelectorAll("[data-alert-toggle]").forEach((cb) => {
      cb.addEventListener("change", async () => {
        // No redraw needed here (unlike delete) -- the checkbox's own
        // state already reflects the intended value, and re-fetching
        // would just flash the list for no visible change. The frequency
        // select next to it just gets enabled/disabled in place to match
        // (it's meaningless while alerts are off, but there's no reason
        // to lose the choice -- re-enabling the checkbox should bring
        // back whatever frequency was already selected).
        await fetch(`/api/saved-searches/${cb.dataset.alertToggle}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ alerts_enabled: cb.checked }),
        });
        const freqSelect = list.querySelector(`[data-frequency-select="${cb.dataset.alertToggle}"]`);
        if (freqSelect) freqSelect.disabled = !cb.checked;
      });
    });
    list.querySelectorAll("[data-frequency-select]").forEach((sel) => {
      sel.addEventListener("change", async () => {
        await fetch(`/api/saved-searches/${sel.dataset.frequencySelect}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ alert_frequency: sel.value }),
        });
      });
    });
  } catch (e) {
    document.getElementById("saved-searches-list").innerHTML = `<div class="empty-modal-state">Couldn't load saved searches.</div>`;
  }
}

// ---- accounts: applied-job tracking ----

// Mirrors db_users.APPLICATION_STATUSES in order -- a hardcoded copy
// rather than a fetched one, since this list changes about as often as
// the rest of the UI copy around it, and a whole endpoint just to serve
// six strings isn't worth the round trip on every modal open.
const APPLICATION_STATUSES = [
  { value: "applied", label: "Applied" },
  { value: "interviewing", label: "Interviewing" },
  { value: "offer", label: "Offer" },
  { value: "rejected", label: "Rejected" },
  { value: "ghosted", label: "Ghosted" },
  { value: "withdrawn", label: "Withdrawn" },
];
const STATUS_LABELS = Object.fromEntries(APPLICATION_STATUSES.map((s) => [s.value, s.label]));

// Which status chip is active in the "My Applications" modal -- plain
// module state rather than anything persisted, since it's cheap to lose
// on refresh and reopening the modal is the normal way to reset it.
let myApplicationsFilter = "all";

async function toggleApplied(jobUrl, btnEl) {
  const wasApplied = btnEl.classList.contains("is-applied");
  btnEl.disabled = true;
  try {
    const res = await fetch("/api/applied-jobs", {
      method: wasApplied ? "DELETE" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_url: jobUrl }),
    });
    if (!res.ok) throw new Error("request failed");
    btnEl.classList.toggle("is-applied", !wasApplied);
    btnEl.textContent = !wasApplied ? "✓ Applied" : "Mark applied";
  } catch (e) {
    // Leave the button in its prior state on failure -- no silent state
    // drift between what's shown and what's actually saved server-side.
  } finally {
    btnEl.disabled = false;
  }
}

async function updateApplicationStatus(jobUrl, status, selectEl) {
  const prevStatus = selectEl.dataset.prevStatus || "applied";
  selectEl.disabled = true;
  try {
    const res = await fetch("/api/applied-jobs/status", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_url: jobUrl, status }),
    });
    if (!res.ok) throw new Error("request failed");
    // Simplest correct redraw -- refetch rather than hand-patch the DOM,
    // same pattern as the delete/unmark handlers below. This also keeps
    // the filter-chip counts and the currently-filtered row list in sync
    // with the change, not just the one dropdown that was touched.
    openMyApplicationsModal();
  } catch (e) {
    selectEl.value = prevStatus; // request failed -- snap the dropdown back rather than show a status that isn't actually saved
    selectEl.disabled = false;
  }
}

async function openMyApplicationsModal() {
  openModal(`<h2 class="modal-title">My applications</h2><div id="applied-jobs-list">Loading…</div>`, { wide: true });
  try {
    const res = await fetch("/api/applied-jobs/full");
    const data = await res.json();
    const jobs = data.jobs || [];
    const list = document.getElementById("applied-jobs-list");
    if (!jobs.length) {
      list.innerHTML = `<div class="empty-modal-state">Nothing marked applied yet. Use "Mark applied" on any job card.</div>`;
      return;
    }

    // Counts per stage power both the filter chips below and the "which
    // stage is everything actually in" overview a flat list didn't give
    // -- the whole point of this pass, since a bare list of applied URLs
    // wasn't "explicitly helpful" once there were more than a handful.
    const counts = { all: jobs.length };
    APPLICATION_STATUSES.forEach((s) => { counts[s.value] = 0; });
    jobs.forEach((j) => {
      const s = j.status || "applied";
      counts[s] = (counts[s] || 0) + 1;
    });

    const chipDefs = [{ value: "all", label: "All" }, ...APPLICATION_STATUSES];
    const chipsHtml = chipDefs
      .map((s) => {
        const active = s.value === myApplicationsFilter ? " active" : "";
        return `<button type="button" class="status-filter-chip status-${s.value}${active}" data-filter="${s.value}">${s.label} <span class="status-filter-count">${counts[s.value] || 0}</span></button>`;
      })
      .join("");

    const visible = myApplicationsFilter === "all"
      ? jobs
      : jobs.filter((j) => (j.status || "applied") === myApplicationsFilter);

    const rowsHtml = visible.length
      ? visible.map(appliedJobRow).join("")
      : `<div class="empty-modal-state">Nothing in "${STATUS_LABELS[myApplicationsFilter] || "All"}" right now.</div>`;

    list.innerHTML = `<div class="status-filter-row">${chipsHtml}</div>${rowsHtml}`;

    list.querySelectorAll("[data-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        myApplicationsFilter = btn.dataset.filter;
        openMyApplicationsModal();
      });
    });
    list.querySelectorAll("[data-unapply]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await fetch("/api/applied-jobs", {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ job_url: btn.dataset.unapply }),
        });
        openMyApplicationsModal();
      });
    });
    list.querySelectorAll("[data-status-select]").forEach((sel) => {
      sel.dataset.prevStatus = sel.value;
      sel.addEventListener("change", () => updateApplicationStatus(sel.dataset.statusSelect, sel.value, sel));
    });
  } catch (e) {
    document.getElementById("applied-jobs-list").innerHTML = `<div class="empty-modal-state">Couldn't load your applications.</div>`;
  }
}

function appliedJobRow(job) {
  const appliedDate = job.applied_at ? String(job.applied_at).slice(0, 10) : "";
  const status = job.status || "applied";
  // `delisted` jobs (see /api/applied-jobs/full in app.py) have no title/
  // company anymore -- the posting closed and dropped out of the live
  // dataset -- so they still show up here (this is the user's application
  // HISTORY, not "still-open postings"), just with the bare URL instead.
  const titleHtml = job.delisted
    ? `<span class="applied-job-title">${escapeHtml(job.url)} <span class="delisted-tag">(no longer listed)</span></span>`
    : `<span class="applied-job-title"><a href="${escapeAttr(job.url)}" target="_blank" rel="noopener">${escapeHtml(job.title)}</a> — ${escapeHtml(job.company || "")}</span>`;
  const statusOptionsHtml = APPLICATION_STATUSES
    .map((s) => `<option value="${s.value}"${s.value === status ? " selected" : ""}>${s.label}</option>`)
    .join("");
  return `
    <div class="applied-job-row">
      ${titleHtml}
      <span class="applied-job-meta">Applied ${escapeHtml(appliedDate)}</span>
      <select class="status-select status-${status}" data-status-select="${escapeAttr(job.url)}" aria-label="Application status">${statusOptionsHtml}</select>
      <button type="button" class="row-action-btn danger" data-unapply="${escapeAttr(job.url)}">Remove</button>
    </div>
  `;
}

// ---- analytics (optional GA4) ----

// Injected at runtime rather than baked into index.html, so the
// measurement ID lives only in an env var (GA_MEASUREMENT_ID on the
// server), not committed to the repo -- same reasoning as
// turnstileSiteKey. A deployment with it unset just never adds the
// script tags below, and this whole thing is a silent no-op.
//
// Contact is now its own page (static/contact.html, same treatment as
// /faq) rather than a modal -- it fetches /api/site-config itself to
// decide whether to show the real form or an "unavailable" message, so
// this page no longer needs contact_enabled at all (and /api/site-config
// never returns contact_email in the first place -- see app.py).
async function loadSiteConfig() {
  try {
    const res = await fetch("/api/site-config");
    const data = await res.json();

    const gaId = data.ga_measurement_id;
    if (!gaId) return;

    const gtagScript = document.createElement("script");
    gtagScript.async = true;
    gtagScript.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(gaId)}`;
    document.head.appendChild(gtagScript);

    window.dataLayer = window.dataLayer || [];
    function gtag() { window.dataLayer.push(arguments); }
    window.gtag = gtag;
    gtag("js", new Date());
    gtag("config", gaId);
  } catch (e) {
    // Analytics is a nice-to-have -- fail silent, same as every other
    // config fetch here.
  }
}

// ---- Hunter (AI search assistant) ----

// Hunter is a free-text chat interface: type a full sentence, and
// hunterParseMessage() below pulls out whatever it recognizes (salary,
// years of experience, department, commitment type, location, recency)
// via regex/keyword matching, updates the real filter controls live, and
// replies in character. No external API calls, nothing to configure --
// every line Hunter says comes from the template pools in this file.
//
// Runs inside the existing modal system (openModal()/closeModal(), same
// one FAQ/Contact/auth use). Because modal content is destroyed and
// rebuilt every time the modal opens (see openModal()'s innerHTML
// replace), the elements below are `let`s re-queried each time
// openHunterModal() runs, not one-time consts.

let assistantMessages, assistantForm, assistantInput, hunterResumeInput, hunterResumeBtn;
let hunterTypingEl = null;
let hunterState = null; // current run's collected answers; null until first opened

function resetHunterState() {
  hunterState = {
    query: "",
    currency: "USD",
    salaryMin: undefined,
    yoeMin: undefined,
    departments: [],
    commitment: "",
    days: "",
    queryCaptured: false, // true once `query` has been set once -- see hunterHandleMessage()
  };
}

function addAssistantMessage(role, text) {
  const div = document.createElement("div");
  div.className = `assistant-msg assistant-msg-${role}`;
  div.textContent = text;
  assistantMessages.appendChild(div);
  assistantMessages.scrollTop = assistantMessages.scrollHeight;
  return div;
}

function hunterSay(text) { addAssistantMessage("assistant", text); }
function userSay(text) { addAssistantMessage("user", text); }

function pickOne(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

// A "Hunter is typing…" bubble shown for a short, length-scaled delay
// before each reply -- purely cosmetic (the parsing itself is instant),
// but an instantaneous reply to a full sentence reads as obviously
// mechanical, and a small pause reads as "considering it."
function hunterTypingStart() {
  hunterTypingEl = document.createElement("div");
  hunterTypingEl.className = "assistant-msg assistant-msg-assistant assistant-typing";
  hunterTypingEl.innerHTML = "<span></span><span></span><span></span>";
  assistantMessages.appendChild(hunterTypingEl);
  assistantMessages.scrollTop = assistantMessages.scrollHeight;
}
function hunterTypingStop() {
  if (hunterTypingEl) { hunterTypingEl.remove(); hunterTypingEl = null; }
}
function hunterReply(text) {
  hunterTypingStart();
  const delay = 300 + Math.min(text.length * 7, 850) + Math.random() * 220;
  return new Promise((resolve) => {
    setTimeout(() => {
      hunterTypingStop();
      hunterSay(text);
      resolve();
    }, delay);
  });
}

// Currencies are for the person's own reference, not converted -- the
// dataset doesn't track a per-posting currency (salary_min/max in db.py
// are plain numbers), so a currency mentioned in chat just changes the
// symbol shown, not what actually gets compared against the (effectively
// USD-denominated) listing data.
const CURRENCY_OPTIONS = [
  { code: "USD", symbol: "$" },
  { code: "EUR", symbol: "€" },
  { code: "GBP", symbol: "£" },
  { code: "CAD", symbol: "C$" },
];
function currencySymbol(code) {
  return (CURRENCY_OPTIONS.find((c) => c.code === code) || CURRENCY_OPTIONS[0]).symbol;
}
function formatAmountShort(n) {
  return `${Math.round(n / 1000)}k`;
}

// Canonical department labels (must match DEPARTMENT_DISPLAY_ORDER in
// department_groups.py) mapped to phrases that signal them in a typed
// message. Checked against the message with the salary/YOE/commitment/
// days/location phrases already stripped out (see hunterParseMessage()),
// so "engineering" in "5 years of engineering experience" doesn't also
// have to fight the YOE regex for the same substring.
// US state names/abbreviations, all lowercased -- used as a precision gate
// for the "City, ST" / "City, State" location pattern below: any
// "<words>, <this>" match only counts as a location if the part after the
// comma resolves to a real state, which is what lets that pattern be
// case-insensitive and fairly loose about the city part without matching
// every random comma in a sentence.
const US_STATE_MAP = {
  al: "AL", ak: "AK", az: "AZ", ar: "AR", ca: "CA", co: "CO", ct: "CT", de: "DE",
  fl: "FL", ga: "GA", hi: "HI", id: "ID", il: "IL", in: "IN", ia: "IA", ks: "KS",
  ky: "KY", la: "LA", me: "ME", md: "MD", ma: "MA", mi: "MI", mn: "MN", ms: "MS",
  mo: "MO", mt: "MT", ne: "NE", nv: "NV", nh: "NH", nj: "NJ", nm: "NM", ny: "NY",
  nc: "NC", nd: "ND", oh: "OH", ok: "OK", or: "OR", pa: "PA", ri: "RI", sc: "SC",
  sd: "SD", tn: "TN", tx: "TX", ut: "UT", vt: "VT", va: "VA", wa: "WA", wv: "WV",
  wi: "WI", wy: "WY", dc: "DC",
  alabama: "AL", alaska: "AK", arizona: "AZ", arkansas: "AR", california: "CA",
  colorado: "CO", connecticut: "CT", delaware: "DE", florida: "FL", georgia: "GA",
  hawaii: "HI", idaho: "ID", illinois: "IL", indiana: "IN", iowa: "IA", kansas: "KS",
  kentucky: "KY", louisiana: "LA", maine: "ME", maryland: "MD", massachusetts: "MA",
  michigan: "MI", minnesota: "MN", mississippi: "MS", missouri: "MO", montana: "MT",
  nebraska: "NE", nevada: "NV", "new hampshire": "NH", "new jersey": "NJ",
  "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
  ohio: "OH", oklahoma: "OK", oregon: "OR", pennsylvania: "PA", "rhode island": "RI",
  "south carolina": "SC", "south dakota": "SD", tennessee: "TN", texas: "TX",
  utah: "UT", vermont: "VT", virginia: "VA", washington: "WA", "west virginia": "WV",
  wisconsin: "WI", wyoming: "WY",
};
function titleCaseWords(s) {
  return s.replace(/\S+/g, (w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase());
}
// The "City, ST" pattern below captures up to 3 words before the comma
// (to allow multi-word cities like "San Francisco"), which also means it
// happily captures leading filler ("in Portland", "the city is Portland")
// as part of the "city" -- this strips those words back off so the
// location label reads "Portland, OR" instead of "The City Is Portland, OR".
const CITY_LEADING_FILLER_RE = /^(?:in|near|around|based|located|the|city|is|for|a|an)\s+/i;
function stripCityFiller(phrase) {
  let p = phrase.trim();
  let prev;
  do {
    prev = p;
    p = p.replace(CITY_LEADING_FILLER_RE, "").trim();
  } while (p !== prev && p.length);
  return p || phrase.trim();
}

const HUNTER_DEPT_KEYWORDS = {
  "Engineering": ["engineering", "engineer", "swe", "software developer", "software eng", "backend", "front end", "frontend", "full stack", "fullstack", "devops", "site reliability"],
  "Product": ["product manager", "product management", "product role", "product team", "pm role"],
  "Design": ["design", "ux", "ui", "user experience"],
  "Sales": ["sales", "account executive", "business development", "bdr", "sdr", "go to market", "go-to-market"],
  "Marketing": ["marketing", "growth marketing", "brand", "content marketing", "seo"],
  "Customer Success": ["customer success", "customer support", "client services"],
  "Operations": ["operations", "business operations", "supply chain", "logistics"],
  "Data": ["data science", "data analyst", "data analytics", "data engineer", "business intelligence"],
  "IT": ["information technology", "helpdesk", "help desk", "it support"],
  "Finance": ["finance", "accounting", "fp&a", "fp and a"],
  "People": ["human resources", "recruiting", "recruiter", "talent acquisition", "people team"],
  "Legal": ["legal", "compliance"],
  "Professional Services": ["professional services", "consulting", "implementation"],
  "Executive": ["executive", "chief of staff"],
};

// Words that should never be mistaken for a city name by the trigger-word
// location pattern below (e.g. "in engineering", "in sales" shouldn't
// become a location just because they follow "in") -- mostly single-word
// department/commitment terms plus generic filler.
const HUNTER_STOPWORDS = new Set([
  "a", "an", "the", "that", "this", "your", "my", "our", "us", "them", "it",
  "one", "some", "any", "full", "part", "contract", "remote", "office",
  "person", "people", "charge", "favor", "engineering", "engineer", "sales",
  "marketing", "design", "product", "finance", "legal", "operations", "data",
  "support", "service", "services", "recruiting", "hr",
]);

// Phrases that, if a typed message asks something like this, get a light
// in-character deflection instead of a straight answer -- Hunter neither
// claims to be a specific real AI model nor volunteers that it's a
// scripted parser; it just steers back to the search. See README's
// "Hunter" section for the reasoning.
const HUNTER_IDENTITY_RE = /\bare you (?:a |an )?(?:real |actual |true )?(?:ai\b|a\.?i\.?\b|bot\b|robot\b|human\b|person\b|chatgpt\b|gpt\b|llm\b)|\bwho (?:made|built|created) you\b|\bwhat model are you\b|\bare you (?:chatgpt|claude|gpt)\b/i;
const HUNTER_IDENTITY_DEFLECTIONS = [
  "Ha — let's keep the spotlight on your job search. What role are you after?",
  "I'd rather find you a great role than talk about myself. What are you looking for?",
  "That's a conversation for another day. Salary, location, role — what's next?",
  "Let's stay focused on you for now — what kind of job can I help you track down?",
];

const HUNTER_RESTART_RE = /\b(restart|start over|reset|new search)\b/i;
// A bare "yes"/"sure"/"ok" is, in context, almost always answering
// Hunter's own "want to add more, or search now?" -- included in
// finalize rather than treated as unparseable filler, since Hunter always
// asks that exact question after every reply.
const HUNTER_FINALIZE_RE = /\b(search now|run (?:it|the search)|go ahead|do it|that'?s (?:it|all|everything)|looks good|find (?:it|them|jobs)|show me|i'?m (?:done|ready)|let'?s go|just search|search please|pull (?:it|them) up)\b|^\s*(?:yes|yeah|yep|yup|sure|sounds good|ok|okay|please|go for it)[.!]?\s*$/i;
const HUNTER_FILLER_RE = /^(no|none|nothing|nope|na|n\/a|meh|idk|not really)\.?!?$/i;

// Small-talk handling -- a real conversational assistant should be able
// to say hi, say you're welcome, and answer "so what have you got so
// far" without those falling into the generic "didn't catch a filter"
// bucket. Each is only handled specially when the message carries no
// other parseable content (see the `bits.length` gate around each check
// in hunterHandleMessage) -- "hey, remote roles please" still applies the
// remote filter, it just also gets a warmer opener.
// Allows an optional trailing "hunter" (the assistant's own name) after
// the greeting word -- "hey hunter", "hi hunter!" -- which the original
// strict full-string match missed entirely: a real user typed exactly
// that and got it parsed as search query text ("hey hunter" roles)
// instead of a greeting, since addressing Hunter by name is one of the
// most natural ways to say hello to it.
const HUNTER_GREETING_RE = /^\s*(?:hi|hello|hey|yo|hiya|good morning|good afternoon|good evening)[!.,]?\s*(?:hunter)?[!.,]?\s*$/i;
const HUNTER_GREETING_REPLIES = [
  "Hey! What kind of role are you after?",
  "Hi there — tell me what you're looking for.",
  "Hello! Give me a role, location, salary, anything to start with.",
];
const HUNTER_THANKS_RE = /\b(thanks|thank you|thx|ty|appreciate it)\b/i;
const HUNTER_THANKS_REPLIES = [
  "Anytime! Anything else, or ready to search?",
  "You're welcome — more filters, or should I run it?",
  "No problem. Want to add anything else, or search now?",
];
const HUNTER_SMALLTALK_RE = /\bhow are you\b|\bhow'?s it going\b|\bwhat'?s up\b/i;
const HUNTER_SMALLTALK_REPLIES = [
  "Doing well, thanks! What are you searching for?",
  "Can't complain — let's find you a role. What are you looking for?",
];
// Covers a message that's really just correcting/waving off the
// previous turn -- "no I was just saying hello", "just kidding", "never
// mind" -- which used to fall through to the generic "not sure what to
// do with that one" reply even though nothing was actually wrong. Kept
// separate from HUNTER_GREETING_RE since these aren't greetings
// themselves, they're clarifying that an earlier message wasn't meant
// as a filter.
const HUNTER_CASUAL_RE = /\b(?:just|only)\s+(?:saying|say)\s+(?:hi|hello|hey)\b|\bnever ?mind\b|\bjust (?:kidding|joking)\b|\bno reason\b/i;
const HUNTER_CASUAL_REPLIES = [
  "All good! Whenever you're ready, tell me what you're looking for.",
  "No worries. I'm here whenever you want to start a search.",
  "Ha, fair enough. Let me know when you've got a role in mind.",
];
const HUNTER_STATUS_RE = /\bwhat (?:have i|do you have|did i (?:say|tell you)|'?ve i got)\b|\bwhat do you have (?:so far)?\b|\bwhat'?s in (?:my|the) search\b|\bshow me what you have\b|\bsummar(?:y|ize)\b/i;

// A message ending in "?" (or matching one of these common phrasings) is
// almost always a question directed AT Hunter ("what else should I
// enter?", "what search terms should I use?"), not job-search info to
// extract -- treated as a real question, answered directly, and never
// allowed to become (part of) the search query. This is the fix for a
// real bug: earlier, any unrecognized text -- including a typed question
// -- got folded into the query and sent straight to the boolean search,
// which reliably returned zero results.
const HUNTER_HELP_RE = /\bwhat (else )?(should|can|do) i (enter|type|say|add|tell you)\b|\bwhat (other )?(search )?terms\b|\bwhat can you do\b|\bhow does this work\b|^\s*help\s*\??\s*$/i;
const HUNTER_HELP_REPLY = "I can pick up things like a salary (\"150k+\"), a city or \"remote\", years of experience (\"senior\", \"5+ years\"), a department (\"engineering\", \"sales\"), a commitment type (\"full-time\", \"contract\"), or how recently it was posted (\"this week\"). Give me any combination, or just say \"search now\" to run what we've got.";

// The core parser. Takes one typed message and returns everything it
// could pull out of it, plus `leftover` -- whatever text is left after
// stripping the recognized salary/YOE/commitment/recency/location phrases
// (department mentions are detected but NOT stripped, since a phrase like
// "product manager" is both a department signal AND useful title text for
// the actual keyword search). `leftover` becomes (part of) the search
// query; everything else becomes a real filter.
function hunterParseMessage(rawText) {
  let working = rawText;
  const result = { departments: [], locationsAdded: [] };

  // ---- salary ----
  const sixFigRe = /\bsix figures?\b/i;
  const kAmtRe = /\$?\s?(\d{2,3}(?:\.\d+)?)\s?k\b/i;
  const commaAmtRe = /\$?\s?(\d{2,3},\d{3})\b/;
  if (sixFigRe.test(working)) {
    result.salaryMin = 100000;
    working = working.replace(sixFigRe, "");
  } else {
    const km = working.match(kAmtRe);
    if (km) {
      result.salaryMin = Math.round(parseFloat(km[1]) * 1000);
      working = working.replace(kAmtRe, "");
    } else {
      const cm = working.match(commaAmtRe);
      if (cm) {
        const n = parseInt(cm[1].replace(",", ""), 10);
        if (n >= 20000 && n <= 2000000) {
          result.salaryMin = n;
          working = working.replace(commaAmtRe, "");
        }
      } else {
        const bm = working.match(/\b(\d{6})\b/);
        if (bm) {
          const n = parseInt(bm[1], 10);
          if (n >= 20000 && n < 1000000) {
            result.salaryMin = n;
            working = working.replace(bm[0], "");
          }
        }
      }
    }
  }
  // Mops up the other side of a typed range ("100k-150k" only had its
  // first match consumed above) and any stray currency word left behind.
  working = working.replace(/\$?\s?\d{2,3}(?:\.\d+)?\s?k\b/gi, "").replace(/\b(?:usd|eur|gbp|cad|dollars?)\b/gi, "");
  const currencyHit = rawText.match(/\b(usd|eur|gbp|cad)\b/i) || rawText.match(/[€£]|\bC\$/);
  if (currencyHit) {
    const c = (currencyHit[1] || currencyHit[0]).toUpperCase();
    if (c === "€" || c === "EUR") result.currency = "EUR";
    else if (c === "£" || c === "GBP") result.currency = "GBP";
    else if (c === "C$" || c === "CAD") result.currency = "CAD";
    else if (c === "USD") result.currency = "USD";
  }

  // ---- years of experience ----
  const yoeBuckets = [
    [/\bentry[\s-]?level\b|\bnew grad(?:uate)?\b|\bno experience\b|\bearly[\s-]?career\b/i, 0],
    [/\bjunior\b|\bassociate\b/i, 1],
    [/\bmid[\s-]?level\b|\bmid[\s-]?career\b/i, 3],
    [/\bsenior\b|\bsr\.\b/i, 6],
    [/\bstaff\b|\bprincipal\b/i, 10],
  ];
  let yoeMatched = false;
  for (const [re, val] of yoeBuckets) {
    if (re.test(working)) {
      result.yoeMin = val;
      working = working.replace(re, "");
      yoeMatched = true;
      break;
    }
  }
  if (!yoeMatched) {
    const ym = working.match(/(\d{1,2})\+?\s*(?:years?|yrs?)(?:\s+of\s+experience)?\b/i);
    if (ym) {
      result.yoeMin = parseInt(ym[1], 10);
      working = working.replace(ym[0], "");
    }
  }

  // ---- commitment ----
  const commitBuckets = [
    [/\bfull[\s-]?time\b|\bfte\b/i, "full"],
    [/\bpart[\s-]?time\b/i, "part"],
    [/\bcontract(?:or)?\b|\bfreelance\b|\btemp(?:orary)?\b/i, "contract"],
    [/\bintern(?:ship)?\b/i, "intern"],
  ];
  for (const [re, bucket] of commitBuckets) {
    if (re.test(working)) {
      const opt = Array.from(commitmentSelect.options).find((o) => o.value && o.text.toLowerCase().includes(bucket));
      if (opt) result.commitment = { value: opt.value, label: opt.text };
      working = working.replace(re, "");
      break;
    }
  }

  // ---- recency ----
  const dayBuckets = [
    [/\btoday\b|\blast 24 hours\b|\bpast day\b/i, "1", "Last 24 hours"],
    [/\b(?:last|past)\s*3\s*days?\b/i, "3", "Last 3 days"],
    [/\bthis week\b|\bpast week\b|\b(?:last|past)\s*(?:7\s*days?|week)\b/i, "7", "Last 7 days"],
    [/\b(?:last|past)\s*(?:2|two)\s*weeks?\b|\b14\s*days?\b/i, "14", "Last 14 days"],
    [/\b(?:this|last|past)\s*month\b|\b30\s*days?\b/i, "30", "Last 30 days"],
  ];
  for (const [re, value, label] of dayBuckets) {
    if (re.test(working)) {
      result.days = { value, label };
      working = working.replace(re, "");
      break;
    }
  }

  // ---- locations ----
  if (/\bremote\b/i.test(working)) {
    result.locationsAdded.push({ type: "group", value: "remote_us", label: locationGroupLabels.remote_us || "Remote (US)" });
    working = working.replace(/\bremote\b/gi, "");
  }
  locationGroupList.forEach((g) => {
    if (g.key === "remote_us") return;
    const label = g.label.toLowerCase();
    if (label.length > 3 && working.toLowerCase().includes(label)) {
      result.locationsAdded.push({ type: "group", value: g.key, label: g.label });
      working = working.replace(new RegExp(g.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), "");
    }
  });
  // "City, ST" / "City, State" -- deliberately case-insensitive and loose
  // about the city part (unlike the trigger-word pattern below), because
  // the part after the comma resolving to a real US state is itself
  // strong evidence this is a location, not a coincidental comma
  // elsewhere in the sentence. This is the pattern that actually needs to
  // catch how people type real locations: "portland, or", "Austin TX",
  // typed lowercase, no "in"/"near" in front of it at all.
  const cityStateRe = /\b([a-zA-Z][a-zA-Z]*(?:\s+[a-zA-Z]+){0,2}),\s*([a-zA-Z]{2}|[a-zA-Z]+(?:\s+[a-zA-Z]+)?)\b/g;
  let csm;
  while ((csm = cityStateRe.exec(working)) !== null) {
    const stateKey = csm[2].trim().toLowerCase();
    const stateAbbr = US_STATE_MAP[stateKey];
    if (!stateAbbr) continue;
    const city = stripCityFiller(csm[1]);
    if (!city || HUNTER_STOPWORDS.has(city.toLowerCase())) continue;
    const label = `${titleCaseWords(city)}, ${stateAbbr}`;
    result.locationsAdded.push({ type: "text", value: label, label });
    working = working.replace(csm[0], "");
  }
  // Bare major-metro name, no state needed at all -- "san francisco",
  // "austin", "new york" -- resolved against the ~68-city curated list in
  // metro_areas.py (see loadMetroCities()/metroCityMap/metroCityRe).
  // Deliberately runs AFTER the "City, ST" pattern above, so "Austin, TX"
  // is already fully consumed as a unit before this looks for bare
  // mentions; only genuinely state-less mentions reach here.
  if (metroCityRe) {
    metroCityRe.lastIndex = 0;
    let mcm;
    while ((mcm = metroCityRe.exec(working)) !== null) {
      const state = metroCityMap[mcm[1].toLowerCase()];
      if (!state) continue;
      const label = `${titleCaseWords(mcm[1])}, ${state}`;
      result.locationsAdded.push({ type: "text", value: label, label });
      working = working.replace(mcm[0], "");
      metroCityRe.lastIndex = 0; // string just changed length -- restart the scan over `working`
    }
  }
  // Short colloquial city nicknames -- "sf", "nyc", "la", "dc" -- that
  // aren't the city's real name so they can't live in the metro-city
  // list above. Runs right after it for the same reason: a real city
  // name should win over a nickname if somehow both appear, though in
  // practice these never overlap. See CITY_ABBR_LABELS for the exact set
  // and why each one is safe to match unconditionally.
  CITY_ABBR_RE.lastIndex = 0;
  let cam;
  while ((cam = CITY_ABBR_RE.exec(working)) !== null) {
    const label = CITY_ABBR_LABELS[cam[1].toLowerCase()];
    if (!label) continue;
    result.locationsAdded.push({ type: "text", value: label, label });
    working = working.replace(cam[0], "");
    CITY_ABBR_RE.lastIndex = 0; // string just changed length -- restart the scan over `working`
  }
  // Trigger-word phrasing ("in Traverse City", "near that little town")
  // -- case-insensitive so it isn't tripped up by how someone actually
  // capitalizes while typing, gated by HUNTER_STOPWORDS so common non-
  // place words right after "in" ("in engineering", "in sales") don't get
  // mistaken for a city. This is the last-resort fallback for real
  // locations outside the curated metro list above.
  const cityRe = /\b(?:in|near|around|based in|located in|city is|city:)\s+([a-zA-Z][a-zA-Z.]+(?:\s+[a-zA-Z.]+){0,2})/gi;
  let cm2;
  while ((cm2 = cityRe.exec(working)) !== null) {
    const city = cm2[1].trim().replace(/[.,]+$/, "");
    if (city.length > 2 && !HUNTER_STOPWORDS.has(city.toLowerCase())) {
      result.locationsAdded.push({ type: "text", value: city, label: titleCaseWords(city) });
      working = working.replace(cm2[0], "");
    }
  }

  // ---- department (detected, not stripped -- see function comment) ----
  const lowerWorking = ` ${working.toLowerCase()} `;
  Object.entries(HUNTER_DEPT_KEYWORDS).forEach(([label, kws]) => {
    if (kws.some((kw) => lowerWorking.includes(kw))) result.departments.push(label);
  });

  // A handful of connector/filler words that tend to survive stripping
  // (e.g. "make it full-time, posted this week" leaves behind "make it
  // posted" once the recognized phrases are gone) but were never
  // meaningful title text to begin with.
  working = working.replace(/\b(?:make it|please make it|posted|listed)\b/gi, " ");

  result.leftover = working.replace(/[,+]/g, " ").replace(/\s{2,}/g, " ").trim();
  return result;
}

// Applies whatever a parse found to the real, live filter state (department
// menu, location chips) -- salary/YOE/commitment/days are staged on
// hunterState and only pushed into their controls at hunterApplyToPage()
// (end of run), same as the old wizard did, since the range sliders only
// need to move once. Locations/departments update live so their chips are
// visible in the chat modal's background immediately, matching how the
// old wizard's location step behaved.
function hunterApplyParsed(parsed) {
  if (parsed.currency) hunterState.currency = parsed.currency;
  if (parsed.salaryMin) hunterState.salaryMin = parsed.salaryMin;
  if (parsed.yoeMin !== undefined) hunterState.yoeMin = parsed.yoeMin;
  if (parsed.commitment) hunterState.commitment = parsed.commitment;
  if (parsed.days) hunterState.days = parsed.days;
  parsed.departments.forEach((d) => { if (!hunterState.departments.includes(d)) hunterState.departments.push(d); });
  let locChanged = false;
  parsed.locationsAdded.forEach((loc) => {
    if (loc.type === "group") {
      if (!selectedLocations.some((s) => s.type === "group" && s.value === loc.value)) {
        selectedLocations.push(loc);
        locChanged = true;
      }
    } else if (!selectedLocations.some((s) => s.type === "text" && s.value.toLowerCase() === loc.value.toLowerCase())) {
      selectedLocations.push(loc);
      locChanged = true;
    }
  });
  if (locChanged) renderLocationChips();
}

// Builds the human-readable recap phrases ("$150k+", "6+ years experience",
// etc.) used in Hunter's replies, from whatever a single parse just found
// (not the full accumulated state) -- so a reply only claims credit for
// what that specific message actually added.
function hunterRecapBits(parsed, { includeQuery = false } = {}) {
  const bits = [];
  if (includeQuery && hunterState.query) bits.push(`"${hunterState.query}" roles`);
  if (parsed.salaryMin) bits.push(`${currencySymbol(hunterState.currency)}${formatAmountShort(parsed.salaryMin)}+`);
  if (parsed.yoeMin !== undefined) bits.push(`${parsed.yoeMin}+ years experience`);
  if (parsed.departments.length) bits.push(parsed.departments.join("/"));
  if (parsed.commitment) bits.push(parsed.commitment.label);
  if (parsed.locationsAdded.length) bits.push(parsed.locationsAdded.map((l) => l.label).join(", "));
  if (parsed.days) bits.push(parsed.days.label.toLowerCase());
  return bits;
}

// Pushes the accumulated hunterState into the real filter controls and
// runs the actual search -- the one place this whole feature bottoms out,
// same role wizardFinish() played in the old click-through version.
// Locations are already live in selectedLocations (see hunterApplyParsed);
// everything else only ever lived in hunterState until now.
function hunterApplyToPage() {
  if (hunterState.query) document.getElementById("q").value = hunterState.query;
  if (hunterState.days) document.getElementById("days").value = hunterState.days.value;
  if (hunterState.commitment) commitmentSelect.value = hunterState.commitment.value;
  if (hunterState.departments.length) {
    selectedDepartments = hunterState.departments.filter((d) => departmentOptions.includes(d));
    renderDepartmentMenu();
  }
  // Both are typed minimums with no ceiling requested -- hi stays at
  // whatever the slider's own real max already is (== "no upper bound"),
  // same convention db.py/search() use everywhere else for an unsent
  // salary_max/yoe_max.
  if (salarySliderCtl && salaryRange && hunterState.salaryMin !== undefined) {
    salaryRange = { ...salaryRange, lo: hunterState.salaryMin, hi: salaryRange.max };
    salarySliderCtl.setValues(hunterState.salaryMin, salaryRange.max);
  }
  if (yoeSliderCtl && yoeRange && hunterState.yoeMin !== undefined) {
    yoeRange = { ...yoeRange, lo: hunterState.yoeMin, hi: yoeRange.max };
    yoeSliderCtl.setValues(hunterState.yoeMin, yoeRange.max);
  }
  search(1);
}

// Plain-English recap of everything accumulated in hunterState/
// selectedLocations SO FAR (not just what one message just added -- see
// hunterRecapBits() for that), for the "what have I got so far?" status
// check below.
function hunterStatusSummary() {
  const bits = [];
  if (hunterState.query) bits.push(`"${hunterState.query}"`);
  if (hunterState.salaryMin !== undefined) bits.push(`${currencySymbol(hunterState.currency)}${formatAmountShort(hunterState.salaryMin)}+`);
  if (hunterState.yoeMin !== undefined) bits.push(`${hunterState.yoeMin}+ years experience`);
  if (hunterState.departments.length) bits.push(hunterState.departments.join("/"));
  if (hunterState.commitment && hunterState.commitment.label) bits.push(hunterState.commitment.label);
  const locLabels = selectedLocations.map((l) => l.label || l.value);
  if (locLabels.length) bits.push(locLabels.join(", "));
  if (hunterState.days && hunterState.days.label) bits.push(hunterState.days.label.toLowerCase());

  if (!bits.length) return "Nothing yet — tell me a role, location, salary, whatever you've got.";
  return `So far I've got: ${bits.join(", ")}. Say "search now" whenever you're ready, or keep adding.`;
}

async function hunterHandleMessage(text) {
  userSay(text);

  if (HUNTER_RESTART_RE.test(text)) {
    await hunterReply(pickOne(["Sure, clean slate.", "No problem, starting fresh.", "Sure thing — starting over."]));
    restartHunter();
    return;
  }

  if (HUNTER_IDENTITY_RE.test(text)) {
    await hunterReply(pickOne(HUNTER_IDENTITY_DEFLECTIONS));
    return;
  }

  const parsed = hunterParseMessage(text);
  hunterApplyParsed(parsed);
  const bits = hunterRecapBits(parsed, { includeQuery: false });

  // Small talk -- only handled specially when the message carries no
  // parseable filter content of its own (a stray "hey, remote roles
  // please" still applies "remote" normally, it just also gets a warmer
  // opener via the reply text below). Checked in this order so a message
  // matching more than one -- unlikely, but "thanks, how's it going" is
  // possible -- resolves to the more specific one first.
  if (!bits.length) {
    if (HUNTER_GREETING_RE.test(text)) { await hunterReply(pickOne(HUNTER_GREETING_REPLIES)); return; }
    if (HUNTER_CASUAL_RE.test(text)) { await hunterReply(pickOne(HUNTER_CASUAL_REPLIES)); return; }
    if (HUNTER_SMALLTALK_RE.test(text)) { await hunterReply(pickOne(HUNTER_SMALLTALK_REPLIES)); return; }
    if (HUNTER_THANKS_RE.test(text)) { await hunterReply(pickOne(HUNTER_THANKS_REPLIES)); return; }
    if (HUNTER_STATUS_RE.test(text)) { await hunterReply(hunterStatusSummary()); return; }
  }

  const finalize = HUNTER_FINALIZE_RE.test(text);
  if (finalize) {
    const closing = bits.length
      ? `Got it — adding ${bits.join(", ")}. Running your search now…`
      : pickOne(["Running your search now…", "On it — pulling that up now…", "Searching now…"]);
    await hunterReply(closing);
    hunterApplyToPage();
    setTimeout(() => closeModal(), 650);
    return;
  }

  // A trailing "?" (or one of the common "what should I type" phrasings)
  // means this message is a question directed at Hunter, not job-search
  // info -- never used as (part of) the query, regardless of whether
  // anything else in it happened to parse as a real filter.
  const isQuestion = /\?\s*$/.test(text.trim()) || HUNTER_HELP_RE.test(text);

  // The query is captured ONCE, from the first message that isn't a
  // question or filler, and never touched again after that. An earlier
  // version kept merging every later message's leftover text into the
  // query too, which meant ordinary chat -- "I live in Phoenix but would
  // also be open to remote roles," "what else should I enter?" -- ended
  // up glued onto the search box and sent straight to the boolean search,
  // reliably returning zero results. Follow-up messages now only ever
  // affect the real filters (salary/YOE/department/commitment/location/
  // days), never the query text.
  const capturingQuery = !isQuestion && !hunterState.queryCaptured;
  if (capturingQuery) {
    hunterState.query = parsed.leftover || "";
    hunterState.queryCaptured = true;
  }

  if (isQuestion && !bits.length) {
    await hunterReply(HUNTER_HELP_REPLY);
    return;
  }

  const recapBits = capturingQuery ? hunterRecapBits(parsed, { includeQuery: true }) : bits;
  let reply;
  if (capturingQuery) {
    reply = recapBits.length
      ? `Got it — ${recapBits.join(", ")}. Want to narrow it down more (salary, location, experience level, department), or just say "search now" and I'll pull it up.`
      : `Got it. Want to narrow it down — salary, location, experience level, department — or just say "search now."`;
  } else if (HUNTER_FILLER_RE.test(text.trim())) {
    reply = pickOne(["No worries — anything else, or say \"search now\"?", "All good — more filters, or ready to search?"]);
  } else if (recapBits.length) {
    reply = pickOne([
      `Noted — added ${recapBits.join(", ")}. Anything else, or should I search now?`,
      `Got it, ${recapBits.join(", ")}. Want to add more, or search now?`,
      `Adding ${recapBits.join(", ")} to the list. More filters, or ready to search?`,
    ]);
  } else {
    reply = pickOne([
      `Didn't catch a specific filter there — try a salary, a city, or something like "senior" or "entry level." Or just say "search now."`,
      `Not sure what to do with that one. Give me a number, a location, or say "search now" whenever you're ready.`,
    ]);
  }
  await hunterReply(reply);
}

function restartHunter() {
  assistantMessages.innerHTML = "";
  resetHunterState();
  hunterGreet();
}

function hunterGreet() {
  hunterSay("Hi, I'm Hunter. Tell me what you're looking for — role, location, salary, experience level, whatever you've got — and I'll set it up. Or attach your resume and I'll start from that instead.");
}

function openHunterModal() {
  openModal(`
    <div class="hunter-header">
      <div class="hunter-avatar">H</div>
      <div>
        <div class="hunter-name">Hunter</div>
        <div class="hunter-tagline">Search assistant</div>
      </div>
    </div>
    <div class="assistant-messages" id="assistant-messages"></div>
    <form id="assistant-form" class="assistant-form">
      <button type="button" id="hunter-resume-btn" class="hunter-attach-btn" title="Attach resume" aria-label="Attach resume">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
      </button>
      <input type="text" id="assistant-input" placeholder="Tell Hunter what you're looking for…" autocomplete="off" />
      <button type="submit" aria-label="Send">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
      </button>
    </form>
  `, { wide: true });

  assistantMessages = document.getElementById("assistant-messages");
  assistantForm = document.getElementById("assistant-form");
  assistantInput = document.getElementById("assistant-input");
  hunterResumeBtn = document.getElementById("hunter-resume-btn");

  // Hidden file input, separate from the main page's #resume-input so
  // choosing a file here doesn't fight over the same <input>'s change
  // listener. Recreated each time the modal opens, same as the fields
  // above.
  hunterResumeInput = document.createElement("input");
  hunterResumeInput.type = "file";
  hunterResumeInput.accept = ".pdf,.docx,.txt";
  hunterResumeInput.hidden = true;
  modalContent.appendChild(hunterResumeInput);

  hunterResumeBtn.addEventListener("click", () => hunterResumeInput.click());

  hunterResumeInput.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = ""; // allow re-selecting the same file later in a restarted run
    if (!file) return;
    userSay(`Attached ${file.name}`);
    await hunterReply("Reading your resume…");
    await handleResumeFile(file); // sets hasResume/match terms and runs an initial search itself
    hunterState.queryCaptured = true; // resume path already ran a search; free text from here on just adds filters, never rewrites the query
    await hunterReply("Got it — I've run an initial search from that. Want to narrow it down further (salary, location, experience level), or say \"search now\" to leave it as-is?");
  });

  assistantForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = assistantInput.value.trim();
    assistantInput.value = "";
    if (!text) return;
    hunterHandleMessage(text);
  });

  resetHunterState();
  hunterGreet();
}

document.getElementById("ai-search-btn").addEventListener("click", openHunterModal);

// ---- misc UI wiring ----

syntaxHelpBtn.addEventListener("click", () => {
  syntaxHelp.classList.toggle("hidden");
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  search(1);
});

// Resets every filter back to its as-loaded default -- query text, days,
// department/commitment selections, sort, location chips, both range
// sliders, and any resume that's been uploaded (its extracted terms drive
// match-tier badges/sort until cleared, so leaving it in place after
// "Clear search" would silently keep filtering/ranking against it).
// Deliberately does NOT touch a saved search or account state -- just the
// live filter form -- then re-runs an unfiltered search.
function clearSearch() {
  document.getElementById("q").value = "";
  document.getElementById("days").value = "30";
  commitmentSelect.value = "";
  sortSelect.value = "newest";
  document.getElementById("sort-match-option").hidden = true;

  selectedDepartments = [];
  renderDepartmentMenu();

  selectedLocations = [];
  renderLocationChips();

  if (salarySliderCtl && salaryRange) {
    salarySliderCtl.setValues(salaryRange.min, salaryRange.max);
    salaryRange = { ...salaryRange, lo: salaryRange.min, hi: salaryRange.max };
  }
  if (yoeSliderCtl && yoeRange) {
    yoeSliderCtl.setValues(yoeRange.min, yoeRange.max);
    yoeRange = { ...yoeRange, lo: yoeRange.min, hi: yoeRange.max };
  }

  hasResume = false;
  resumeTitleTerms = [];
  resumeSkillTerms = [];
  resumeUsBased = false;
  resumeMetroTerms = [];
  resumeInput.value = "";
  resumeStatus.textContent = "";
  resumeStatus.className = "resume-status";

  search(1);
}

clearSearchBtn.addEventListener("click", clearSearch);

// Sort is a "how do you want to look at what you already have" control,
// not a new query — applying it immediately (unlike the other filters,
// which wait for the Search button) matches how sort dropdowns behave
// everywhere else.
sortSelect.addEventListener("change", () => search(1));

loadFacets();
loadLocationGroups();
loadMetroCities();
loadAuthConfig();
loadStatus();
loadSiteConfig();
checkForResetToken();
setInterval(loadStatus, 30000);
// Waits on the auth check specifically (fast -- one query, or an
// immediate no-op if accounts aren't configured) so the very first
// render already knows whether to show "Mark applied" toggles, instead
// of a flash where they appear only after the next search.
checkAuth().then(() => search(1));
