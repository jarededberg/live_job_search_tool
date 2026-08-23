const form = document.getElementById("search-form");
const resultsEl = document.getElementById("results");
const statusLine = document.getElementById("status-line");
const paginationEl = document.getElementById("pagination");
const footerStatus = document.getElementById("footer-status");
const departmentSelect = document.getElementById("department");
const commitmentSelect = document.getElementById("commitment");
const locationInput = document.getElementById("location-input");
const locationChipsEl = document.getElementById("location-chips");
const locationDropdown = document.getElementById("location-dropdown");
const locationSelectEl = document.getElementById("location-select");
const syntaxHelpBtn = document.getElementById("syntax-help-btn");
const syntaxHelp = document.getElementById("syntax-help");
const resumeInput = document.getElementById("resume-input");
const resumeDropzone = document.getElementById("resume-dropzone");
const resumeStatus = document.getElementById("resume-status");

let currentPage = 1;
let selectedLocations = [];
let logoCache = {};

async function loadLogoCache() {
  try {
    const res = await fetch("/logo_cache.json");
    logoCache = await res.json();
  } catch (e) {
    logoCache = {}; // logos are cosmetic; fail quietly and just skip them
  }
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

  statusLine.textContent = "Searching…";
  resultsEl.innerHTML = "";
  paginationEl.innerHTML = "";

  const params = new URLSearchParams();
  qs({ q, days, department, commitment, page, per_page: 50 })
    .split("&")
    .filter(Boolean)
    .forEach((pair) => {
      const [k, v] = pair.split("=");
      params.append(decodeURIComponent(k), decodeURIComponent(v));
    });
  selectedLocations.forEach((loc) => params.append("location", loc));

  try {
    const res = await fetch(`/api/jobs?${params.toString()}`);
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

  resultsEl.innerHTML = data.jobs.map(jobRow).join("");

  const prevDisabled = data.page <= 1 ? "disabled" : "";
  const nextDisabled = data.page >= data.pages ? "disabled" : "";
  paginationEl.innerHTML = `
    <button ${prevDisabled} onclick="search(${data.page - 1})">← Prev</button>
    <span class="page-indicator">Page ${data.page.toLocaleString()} of ${data.pages.toLocaleString()}</span>
    <button ${nextDisabled} onclick="search(${data.page + 1})">Next →</button>
  `;
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

function jobRow(job) {
  const posted = job.posted ? job.posted : "date unknown";
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

  return `
    <div class="job-row">
      ${logoHtml(job.company)}
      <div class="job-main">
        <div class="job-title"><a href="${escapeAttr(job.url)}" target="_blank" rel="noopener">${escapeHtml(job.title)}</a></div>
        <div class="job-sub">${escapeHtml(job.company)} · ${escapeHtml(location)}</div>
      </div>
      <div class="job-side">
        <span>${escapeHtml(posted)}</span>
        ${tags.join("")}
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
  } catch (e) {
    /* facets are a nice-to-have; fail quietly */
  }
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
    .map(
      (loc, i) =>
        `<span class="location-chip">${escapeHtml(loc)}<button type="button" data-idx="${i}" aria-label="Remove ${escapeAttr(loc)}">×</button></span>`
    )
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
      .map((v, i) => `<div class="location-option" data-idx="${i}">${escapeHtml(v)}</div>`)
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

function pickLocation(value) {
  if (value && !selectedLocations.includes(value)) {
    selectedLocations.push(value);
    renderLocationChips();
    search(1);
  }
  locationInput.value = "";
  hideLocationDropdown();
}

async function fetchLocationOptions(query) {
  try {
    const res = await fetch(`/api/locations?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    return (data.locations || []).filter((v) => !selectedLocations.includes(v));
  } catch (e) {
    return [];
  }
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
    document.getElementById("q").value = data.query;
    resumeStatus.textContent = `Extracted: ${data.terms.join(", ")} — edit above, then search.`;
    resumeStatus.className = "resume-status ok";
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

// ---- misc UI wiring ----

syntaxHelpBtn.addEventListener("click", () => {
  syntaxHelp.classList.toggle("hidden");
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  search(1);
});

loadFacets();
loadStatus();
setInterval(loadStatus, 30000);
loadLogoCache().then(() => search(1)); // wait for it so the first render already has logos, not a flash-in
