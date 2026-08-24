const form = document.getElementById("search-form");
const resultsEl = document.getElementById("results");
const statusLine = document.getElementById("status-line");
const paginationEl = document.getElementById("pagination");
const footerStatus = document.getElementById("footer-status");
const departmentSelect = document.getElementById("department");
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

async function search(page = 1) {
  currentPage = page;
  const q = document.getElementById("q").value.trim();
  const days = document.getElementById("days").value;
  const department = departmentSelect.value;
  const commitment = commitmentSelect.value;
  const sort = sortSelect.value;

  statusLine.textContent = "Searching…";
  resultsEl.innerHTML = "";
  paginationEl.innerHTML = "";

  const params = new URLSearchParams();
  qs({ q, days, department, commitment, sort, page, per_page: 50 })
    .split("&")
    .filter(Boolean)
    .forEach((pair) => {
      const [k, v] = pair.split("=");
      params.append(decodeURIComponent(k), decodeURIComponent(v));
    });
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
  paginationEl.innerHTML = `
    <button ${prevDisabled} onclick="search(${data.page - 1})">← Prev</button>
    <div class="page-numbers">${numbersHtml}</div>
    <button ${nextDisabled} onclick="search(${data.page + 1})">Next →</button>
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
        ${appliedBtn}
      </div>
    </div>
  `;
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
      const finished = new Date(data.last_run.finished_at);
      footerStatus.textContent =
        `${data.total_jobs.toLocaleString()} open roles cached from ${data.total_companies.toLocaleString()} companies · ` +
        `last refreshed ${finished.toLocaleString()}`;
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
    fillSelect(departmentSelect, data.departments, "Any department");
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

async function loadAuthConfig() {
  try {
    const res = await fetch("/api/auth-config");
    const data = await res.json();
    turnstileSiteKey = data.turnstile_site_key || "";
  } catch (e) {
    turnstileSiteKey = ""; // fail quiet -- same as every other nice-to-have fetch here
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

function openModal(html, { wide = false } = {}) {
  modalContent.innerHTML = html;
  modalBox.classList.toggle("modal-wide", wide);
  modalOverlay.classList.remove("hidden");
}

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
    <h2 class="modal-title">${isLogin ? "Log in" : "Sign up"}</h2>
    <form id="auth-form">
      <label for="auth-email">Email</label>
      <input type="email" id="auth-email" required autocomplete="email" />
      <label for="auth-password">Password</label>
      <input type="password" id="auth-password" required minlength="8"
        autocomplete="${isLogin ? "current-password" : "new-password"}" />
      ${showTurnstile ? `<div id="turnstile-widget"></div>` : ""}
      <div class="auth-error" id="auth-error"></div>
      <button type="submit" class="btn-primary">${isLogin ? "Log in" : "Create account"}</button>
    </form>
    <p class="modal-switch">
      ${isLogin ? "No account?" : "Already have an account?"}
      <a href="#" id="auth-switch">${isLogin ? "Sign up" : "Log in"}</a>
    </p>
  `);
  if (showTurnstile) renderTurnstileWidget();
  document.getElementById("auth-switch").addEventListener("click", (e) => {
    e.preventDefault();
    openAuthModal(isLogin ? "signup" : "login");
  });
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
    department: departmentSelect.value,
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
  if (params.department) departmentSelect.value = params.department;
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
  } catch (e) {
    document.getElementById("saved-searches-list").innerHTML = `<div class="empty-modal-state">Couldn't load saved searches.</div>`;
  }
}

// ---- accounts: applied-job tracking ----

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

async function openMyApplicationsModal() {
  openModal(`<h2 class="modal-title">My applications</h2><div id="applied-jobs-list">Loading…</div>`, { wide: true });
  try {
    const res = await fetch("/api/applied-jobs/full");
    const data = await res.json();
    const list = document.getElementById("applied-jobs-list");
    if (!data.jobs || !data.jobs.length) {
      list.innerHTML = `<div class="empty-modal-state">Nothing marked applied yet. Use "Mark applied" on any job card.</div>`;
      return;
    }
    list.innerHTML = data.jobs.map(appliedJobRow).join("");
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
  } catch (e) {
    document.getElementById("applied-jobs-list").innerHTML = `<div class="empty-modal-state">Couldn't load your applications.</div>`;
  }
}

function appliedJobRow(job) {
  const appliedDate = job.applied_at ? String(job.applied_at).slice(0, 10) : "";
  // `delisted` jobs (see /api/applied-jobs/full in app.py) have no title/
  // company anymore -- the posting closed and dropped out of the live
  // dataset -- so they still show up here (this is the user's application
  // HISTORY, not "still-open postings"), just with the bare URL instead.
  const titleHtml = job.delisted
    ? `<span class="applied-job-title">${escapeHtml(job.url)} <span class="delisted-tag">(no longer listed)</span></span>`
    : `<span class="applied-job-title"><a href="${escapeAttr(job.url)}" target="_blank" rel="noopener">${escapeHtml(job.title)}</a> — ${escapeHtml(job.company || "")}</span>`;
  return `
    <div class="applied-job-row">
      ${titleHtml}
      <span class="applied-job-meta">Applied ${escapeHtml(appliedDate)}</span>
      <button type="button" class="row-action-btn danger" data-unapply="${escapeAttr(job.url)}">Remove</button>
    </div>
  `;
}

// ---- misc UI wiring ----

syntaxHelpBtn.addEventListener("click", () => {
  syntaxHelp.classList.toggle("hidden");
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  search(1);
});

// Sort is a "how do you want to look at what you already have" control,
// not a new query — applying it immediately (unlike the other filters,
// which wait for the Search button) matches how sort dropdowns behave
// everywhere else.
sortSelect.addEventListener("change", () => search(1));

loadFacets();
loadLocationGroups();
loadAuthConfig();
loadStatus();
setInterval(loadStatus, 30000);
// Waits on the auth check specifically (fast -- one query, or an
// immediate no-op if accounts aren't configured) so the very first
// render already knows whether to show "Mark applied" toggles, instead
// of a flash where they appear only after the next search.
checkAuth().then(() => search(1));
