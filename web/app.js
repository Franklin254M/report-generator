"use strict";
/* One page, six states: idle, uploading, done, needs_input, failed, expired.
   The job id lives in the URL (#job=...) so refresh and bookmarks work. */

const MAX_BYTES = 10 * 1024 * 1024;
const view = document.getElementById("view");
const live = document.getElementById("live");
let currentId = null;

// Build DOM safely: strings become text nodes, never HTML.
function h(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(attrs)) {
    if (key === "class") node.className = val;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), val);
    else if (val !== false && val != null) node.setAttribute(key, val === true ? "" : val);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
  return node;
}

class Fail extends Error {
  constructor(info) { super(info.message); this.info = info; }
}
const GENERIC = { code: "unknown", message: "Something went wrong.", fix: "Try again." };

async function api(path, options) {
  let res;
  try { res = await fetch(path, options); }
  catch { throw new Fail({ code: "network", message: "We couldn't reach the server.", fix: "Check your connection and try again." }); }
  if (res.status === 204) return null;
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw new Fail(body && body.error ? body.error : GENERIC);
  return body;
}

function show(node, { idle = false, announce = "" } = {}) {
  document.body.classList.toggle("idle", idle);
  view.replaceChildren(node);
  const heading = node.querySelector("h1, h2");
  if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
  live.textContent = announce;
}

const number = (n) => Number(n).toLocaleString();
const fileButton = (label, href) => h("a", { class: "btn", href, download: "" }, label);

// ------------------------------------------------------------------ idle
function idleView(note) {
  const input = h("input", { type: "file", accept: ".csv,text/csv,.txt", id: "file", "aria-label": "Choose a CSV file" });
  input.addEventListener("change", () => input.files[0] && upload(input.files[0]));
  const zone = h("label", { class: "dropzone", for: "file" },
    h("p", {}, "Drop a CSV here"), h("span", { class: "btn primary" }, "Choose file"), input);
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault(); zone.classList.remove("over");
    if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
  });
  const sample = h("button", { type: "button", class: "linkish", onclick: useSample }, "Try sample file");
  show(h("div", { class: "card narrow" },
    h("h1", {}, "Clean your CSV file"),
    h("p", { class: "lead" }, "Get a cleaned file, a summary, and a log of every change."),
    note ? h("p", { class: "notice" }, note) : null, zone,
    h("div", { class: "row" }, h("span", {}, "Max 10 MB"), sample)), { idle: true });
}

async function useSample() {
  try {
    const res = await fetch("/sample.csv");
    if (!res.ok) throw new Error("no sample");
    upload(new File([await res.blob()], "sample_transactions.csv", { type: "text/csv" }));
  } catch { errorView({ message: "The sample file isn't available.", fix: "Choose your own CSV instead." }); }
}

// ------------------------------------------------------------- uploading
function workingView(title, name) {
  show(h("div", { class: "card narrow" }, h("h1", {}, title),
    h("p", { class: "lead" }, name), h("div", { class: "bar-track" }, h("div", { class: "bar-fill" }))),
    { announce: title });
}

async function upload(file) {
  if (file.size > MAX_BYTES)
    return errorView({ message: "That file is over 10 MB.", fix: "Split it into smaller files." });
  workingView("Cleaning your file…", file.name);
  const form = new FormData();
  form.append("file", file, file.name);
  try { render(await api("/api/jobs", { method: "POST", body: form })); }
  catch (err) { errorView(err.info || GENERIC); }
}

async function rerun(id, body) {
  workingView("Re-running…", "Applying your choice");
  try {
    render(await api(`/api/jobs/${id}/rerun`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
  } catch (err) { errorView(err.info || GENERIC); }
}

// ----------------------------------------------------------------- render
function render(job) {
  currentId = job.id;
  if (location.hash !== `#job=${job.id}`) location.hash = `job=${job.id}`;
  if (job.status === "needs_input") return mappingView(job);
  if (job.status === "done") return doneView(job);
  errorView(job.error || GENERIC);
}

const ORDER = { month: "month-first", day: "day-first" };
const SOURCE = { chosen: "your choice", detected: "detected from the file",
  conflict: "the file mixes both orders, so the default was used", assumed: "the file gives no clue, so the default was used" };
const FIXED = ["duplicates", "duplicate_ids"];
const LABELS = {
  duplicates: "Exact duplicate rows removed", duplicate_ids: "Rows with a repeated ID removed (first kept)",
  missing_dates: "Rows with no date", unreadable_dates: "Dates we couldn't read",
  missing_amounts: "Rows with no amount", unreadable_amounts: "Amounts we couldn't read",
  outliers: "Unusual amounts (kept, but flagged)" };

function listOf(issues, cls) {
  return h("ul", cls ? {} : { class: "list" }, issues.map((i) =>
    h("li", {}, h("span", {}, LABELS[i.type] || i.note), h("span", {}, number(i.count)))));
}

function doneView(job) {
  const issues = job.issues || [];
  const count = (t) => (issues.find((i) => i.type === t) || { count: 0 }).count;
  const fixed = issues.filter((i) => FIXED.includes(i.type));
  const check = issues.filter((i) => i.type in LABELS && !FIXED.includes(i.type));
  const out = (job.stages.find((s) => s.name === "clean") || {}).rows_out;
  const d = job.downloads || {};

  const ambiguous = count("ambiguous_dates");
  const other = job.date_order === "month" ? "day" : "month";
  const side = h("div", { class: "side" },
    h("div", { class: "muted" }, job.input.filename), h("div", { class: "muted" }, `${number(job.input.rows)} rows`),
    h("div", { class: "k" }, "Data quality"),
    h("div", { class: "score" }, String(job.quality_score), h("small", {}, " / 100")),
    h("div", { class: "k" }, "Rows after cleaning"), h("div", { class: "v" }, out == null ? "–" : number(out)));

  const body = h("div", { class: "body" },
    h("h1", {}, "Cleaned and ready"),
    h("p", { class: "lead" }, "Here is what we changed and what needs a look."),
    h("h2", {}, "What we fixed"),
    h("div", {}, fixed.length ? listOf(fixed) : null,
      h("p", { class: "muted" }, "Dates, amounts, and text were made consistent.")),
    check.length ? h("div", { class: "notice" }, h("h2", {}, "Please check these"), listOf(check, true)) : null,
    ambiguous ? h("div", { class: "notice" },
      h("h2", {}, "Some dates could be read two ways"),
      h("p", {}, `${number(ambiguous)} dates such as 03/04/23 were read ${ORDER[job.date_order]} (${SOURCE[job.date_order_source]}). `
        + `If your file uses ${ORDER[other]} dates, switch it.`),
      h("button", { type: "button", class: "btn", onclick: () => rerun(job.id, { date_order: other }) },
        `Read dates ${ORDER[other]}`)) : null,
    (job.warnings || []).map((w) => h("p", { class: "notice" }, w)),
    h("div", { class: "actions" },
      d.xlsx ? h("a", { class: "btn primary", href: d.xlsx, download: "" }, "Download Excel") : null,
      d.csv ? fileButton("Download CSV", d.csv) : null,
      d.log ? fileButton("Cleaning log", d.log) : null,
      d.review ? fileButton("Review list", d.review) : null,
      h("button", { type: "button", class: "btn danger push", onclick: () => remove(job.id) }, "Delete now")),
    h("p", {}, h("button", { type: "button", class: "linkish", onclick: reset }, "Clean another file")));
  show(h("div", { class: "card result" }, side, body), { announce: "Your file is ready." });
}

function mappingView(job) {
  const field = job.needs_input.field;
  const what = field === "date_column" ? "date" : "amount";
  const select = h("select", { id: "col", "aria-label": `Column that holds the ${what}` },
    job.needs_input.options.map((o) => h("option", { value: o }, o)));
  show(h("div", { class: "card narrow" },
    h("h1", {}, `Which column has the ${what}?`),
    h("p", { class: "lead" }, `We couldn't find the ${what} column on our own. Pick it and we'll carry on.`),
    h("div", { class: "map" }, select,
      h("button", { type: "button", class: "btn primary", onclick: () => rerun(job.id, { [field]: select.value }) }, "Re-run")),
    h("button", { type: "button", class: "linkish", onclick: reset }, "Start over")),
    { announce: `Choose the ${what} column.` });
}

function errorView(info) {
  show(h("div", { class: "card narrow" }, h("h1", {}, "That didn't work"),
    h("div", { class: "notice" }, h("p", {}, info.message), h("p", {}, info.fix)),
    h("button", { type: "button", class: "btn primary", onclick: reset }, "Try again")),
    { announce: info.message });
}

function goneView(title, text) {
  show(h("div", { class: "card narrow" }, h("h1", {}, title), h("p", { class: "lead" }, text),
    h("button", { type: "button", class: "btn primary", onclick: reset }, "Upload a new file")), { announce: title });
}

// ---------------------------------------------------------------- actions
function reset() { currentId = null; history.replaceState(null, "", location.pathname); idleView(); }

async function remove(id) {
  if (!confirm("Delete this job and its files now?")) return;
  try { await api(`/api/jobs/${id}`, { method: "DELETE" }); currentId = null;
    history.replaceState(null, "", location.pathname); idleView("Your files were deleted."); }
  catch (err) { errorView(err.info || GENERIC); }
}

async function route() {
  const id = (location.hash.match(/^#job=([0-9a-f-]{36})$/) || [])[1];
  if (!id) { currentId = null; return idleView(); }
  if (id === currentId) return;
  workingView("Loading…", "");
  try { render(await api(`/api/jobs/${id}`)); }
  catch (err) {
    const code = err.info && err.info.code;
    if (code === "job_expired") goneView("This job has expired", "Files are deleted after 24 hours. Upload the file again.");
    else if (code === "job_not_found") goneView("We can't find that job", "The link may be wrong, or the files were deleted.");
    else errorView(err.info || GENERIC);
  }
}

window.addEventListener("hashchange", route);
route();
