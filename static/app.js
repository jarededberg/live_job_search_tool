const form = document.getElementById("search-form");
const resultsEl = document.getElementById("results");
const statusLine = document.getElementById("status-line");
const paginationEl = document.getElementById("pagination");
const footerStatus = document.getElementById("footer-status");

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

  statusLine.textContent = "Searching…";
  resultsEl.innerHTML = "";
  paginationEl.innerHTML = "";

  try {
    const res = await fetch(`/api/jobs?${qs({ q, location, days, page, per_page: 25 })}`);
    const data = await res.json();
    renderResults(data);
  } catch (e) {
    statusLine.textContent = "Something went wrong searching. Try again in a moment.";
  }
}

function renderResults(data) {
  if (!data.jobs.length) {
    resultsEl.innerHTML = `<div class="empty-state">No matching roles found. Try broader keywords or clear the location filter.</div>`;
    statusLine.textContent = `0 results`;
    return;
  }

  statusLine.textContent = `${data.total.toLocaleString()} result${data.total === 1 ? "" : "s"} — page ${data.page} of ${data.pages}`;

  resultsEl.innerHTML = data.jobs.map(jobCard).join("");

  const prevDisabled = data.page <= 1 ? "disabled" : "";
  const nextDisabled = data.page >= data.pages ? "disabled" : "";
  paginationEl.innerHTML = `
    <button ${prevDisabled} onclick="search(${data.page - 1})">← Prev</button>
    <button ${nextDisabled} onclick="search(${data.page + 1})">Next →</button>
  `;
}

function jobCard(job) {
  const posted = job.posted ? job.posted : "date unknown";
  const location = job.location || "Location not listed";
  return `
    <div class="job-card">
      <h3><a href="${escapeAttr(job.url)}" target="_blank" rel="noopener">${escapeHtml(job.title)}</a></h3>
      <div class="job-meta">
        ${escapeHtml(job.company)} · ${escapeHtml(location)} · posted ${escapeHtml(posted)}
        <span class="source-tag">${escapeHtml(job.source)}</span>
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

form.addEventListener("submit", (e) => {
  e.preventDefault();
  search(1);
});

loadStatus();
setInterval(loadStatus, 30000);
search(1);
