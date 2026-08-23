const form = document.getElementById("search-form");
const resultsEl = document.getElementById("results");
const statusLine = document.getElementById("status-line");
const paginationEl = document.getElementById("pagination");
const footerStatus = document.getElementById("footer-status");
const departmentSelect = document.getElementById("department");
const commitmentSelect = document.getElementById("commitment");
const syntaxHelpBtn = document.getElementById("syntax-help-btn");
const syntaxHelp = document.getElementById("syntax-help");
const resumeInput = document.getElementById("resume-input");
const resumeDropzone = document.getElementById("resume-dropzone");
const resumeStatus = document.getElementById("resume-status");

let currentPage = 1;

function qs(params) {
  return Object.entries(params)
    .filter(([, v]) => v !== "" && v !== null && v !== undefined)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");
}

async function search(page = 1) {
  currentPage = page;
  const q = document.getElementById("q").value.trim();
  const location = document.getElementById("location").value.trim();
  const days = document.getElementById("days").value;
  const department = departmentSelect.value;
  const commitment = commitmentSelect.value;

  statusLine.textContent = "Searching…";
  resultsEl.innerHTML = "";
  paginationEl.innerHTML = "";

  try {
    const res = await fetch(`/api/jobs?${qs({ q, location, days, department, commitment, page, per_page: 25 })}`);
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
    return;
  }

  statusLine.textContent = `${data.total.toLocaleString()} result${data.total === 1 ? "" : "s"} — page ${data.page} of ${data.pages}`;
  resultsEl.innerHTML = data.jobs.map(jobCard).join("");

  const prevDisabled = data.page <= 1 ? "disabled" : "";
  const nextDisabled = data.page >= data.pages ? "disabled" : "";
  paginationEl.innerHTML = `
    <button ${prevDisabled} onclick="search(${data.page - 1})">← Prev</button>
    <span class="page-indicator">Page ${data.page} of ${data.pages}</span>
    <button ${nextDisabled} onclick="search(${data.page + 1})">Next →</button>
  `;
}

function jobCard(job) {
  const posted = job.posted ? job.posted : "date unknown";
  const location = job.location || "Location not listed";
  const tags = [`<span class="tag tag-source">${escapeHtml(job.source)}</span>`];
  if (job.department) tags.push(`<span class="tag tag-department">${escapeHtml(job.department)}</span>`);
  if (job.commitment) tags.push(`<span class="tag tag-commitment">${escapeHtml(job.commitment)}</span>`);

  return `
    <div class="job-card">
      <h3><a href="${escapeAttr(job.url)}" target="_blank" rel="noopener">${escapeHtml(job.title)}</a></h3>
      <div class="job-meta">
        <span>${escapeHtml(job.company)} · ${escapeHtml(location)} · posted ${escapeHtml(posted)}</span>
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
search(1);
