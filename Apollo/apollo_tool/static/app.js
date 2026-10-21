const $ = (id) => document.getElementById(id);

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
const ALLOWED_EXTENSIONS = ["xlsx", "xlsm", "csv", "json"];

let uploaded = null;
let current = null;
let pollTimer = null;
let polling = false;
let hiddenLogRunId = null;
let uploadXhr = null;
let uploadSeq = 0;
let initialModalHandled = false;
let lastFocused = null;
let modalCloseTimer = null;
let toastHideTimer = null;
let toastRemoveTimer = null;

const setupModal = $("setupModal");
const shell = $("shell");
const statusPill = $("statusPill");
const statusPillText = $("statusPillText");
const phaseChip = $("phaseChip");
const banner = $("banner");
const startBtn = $("startBtn");
const stopBtn = $("stopBtn");
const hideBrowser = $("hideBrowser");
const limitInput = $("limit");
const logEl = $("log");
const dropzone = $("dropzone");
const fileInput = $("fileInput");
const fileCard = $("fileCard");
const fileBadge = $("fileBadge");
const uploadProgressWrap = $("uploadProgressWrap");
const uploadBar = $("uploadBar");
const uploadPct = $("uploadPct");
const uploadStatusText = $("uploadStatusText");
const uploadError = $("uploadError");
const uploadErrorText = $("uploadErrorText");
const fileDetails = $("fileDetails");
const runSettings = $("runSettings");

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { detail: text }; }
  if (!response.ok) {
    throw new Error(errorMessage(data, `Request failed (${response.status})`));
  }
  return data;
}

function errorMessage(payload, fallback) {
  if (!payload) return fallback;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) {
    return payload.detail.map((item) => item.msg || String(item)).join(", ");
  }
  return fallback;
}

function toast(message, isError = false) {
  const el = $("toast");
  clearTimeout(toastHideTimer);
  clearTimeout(toastRemoveTimer);
  el.textContent = message;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  void el.offsetWidth;
  requestAnimationFrame(() => el.classList.add("show"));
  toastHideTimer = setTimeout(() => {
    el.classList.remove("show");
    toastRemoveTimer = setTimeout(() => { el.hidden = true; }, 180);
  }, 4200);
}

function fmtBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function fmtElapsed(iso) {
  if (!iso) return "—";
  const start = new Date(iso).getTime();
  if (Number.isNaN(start)) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function baseName(path) {
  if (!path) return "";
  return path.split(/[\\/]/).pop();
}

function extensionOf(name) {
  const dot = (name || "").lastIndexOf(".");
  return dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
}

function fileIconLabel(name) {
  const extension = extensionOf(name);
  if (extension === "csv") return "CSV";
  if (extension === "json") return "JSON";
  return "XLS";
}

/* ---------------------------------------------------------------- modal */
function visibleFocusable() {
  return [...setupModal.querySelectorAll(
    'button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])'
  )].filter((el) => el.offsetParent !== null);
}

function openModal() {
  if (current && current.running) {
    toast("A run is already active. Stop it before choosing another file.", true);
    return;
  }
  clearTimeout(modalCloseTimer);
  setupModal.classList.remove("is-closing");
  if (!setupModal.hidden) {
    focusModalTarget();
    return;
  }
  lastFocused = document.activeElement;
  setupModal.hidden = false;
  setupModal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  shell.inert = true;
  shell.setAttribute("aria-hidden", "true");
  requestAnimationFrame(() => {
    setupModal.classList.add("is-open");
    window.setTimeout(focusModalTarget, 220);
  });
}

function focusModalTarget() {
  if (!setupModal.classList.contains("is-open")) return;
  const panel = setupModal.querySelector(".modal-panel");
  if (panel) panel.focus({ preventScroll: true });
}

function closeModal() {
  if (setupModal.hidden || setupModal.classList.contains("is-closing")) return;
  setupModal.classList.remove("is-open");
  setupModal.classList.add("is-closing");
  shell.inert = false;
  shell.removeAttribute("aria-hidden");
  document.body.classList.remove("modal-open");
  modalCloseTimer = setTimeout(() => {
    setupModal.hidden = true;
    setupModal.classList.remove("is-closing");
    setupModal.setAttribute("aria-hidden", "true");
    if (lastFocused && typeof lastFocused.focus === "function" && !lastFocused.disabled) {
      lastFocused.focus({ preventScroll: true });
    }
  }, 190);
}

setupModal.addEventListener("click", (event) => {
  if (event.target.closest("[data-modal-close]")) closeModal();
});

$("closeSetupBtn").addEventListener("click", closeModal);
$("cancelSetupBtn").addEventListener("click", closeModal);
$("openSetupBtn").addEventListener("click", openModal);
$("chooseFileBtn").addEventListener("click", openModal);
$("reviewBtn").addEventListener("click", openModal);

document.addEventListener("keydown", (event) => {
  if (setupModal.hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeModal();
    return;
  }
  if (event.key !== "Tab") return;
  const items = visibleFocusable();
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

window.addEventListener("dragover", (event) => {
  if (!setupModal.hidden) event.preventDefault();
});
window.addEventListener("drop", (event) => {
  if (!setupModal.hidden) event.preventDefault();
});

/* ---------------------------------------------------------------- upload */
function validateFile(file) {
  if (!file) return "Choose a spreadsheet file first.";
  if (!ALLOWED_EXTENSIONS.includes(extensionOf(file.name))) {
    return "Use an .xlsx, .xlsm, .csv or .json file.";
  }
  if (!file.size) return "That file is empty.";
  if (file.size > MAX_UPLOAD_BYTES) return "That file is larger than 25 MB.";
  return "";
}

function resetUploadState() {
  uploadSeq += 1;
  if (uploadXhr) {
    try { uploadXhr.abort(); } catch (_) { /* already finished */ }
    uploadXhr = null;
  }
  uploaded = null;
  fileInput.value = "";
  dropzone.hidden = false;
  fileCard.hidden = true;
  fileCard.classList.remove("is-uploading", "is-ready", "is-error");
  fileBadge.className = "soft-badge";
  fileBadge.textContent = "Uploading";
  uploadProgressWrap.hidden = false;
  uploadBar.style.width = "0%";
  uploadPct.textContent = "0%";
  uploadStatusText.textContent = "Uploading spreadsheet…";
  uploadError.hidden = true;
  uploadErrorText.textContent = "";
  fileDetails.hidden = true;
  runSettings.hidden = true;
  updateStartState();
  updateHint();
  renderWorkbook();
}

function setUploadProgress(percent, label) {
  const value = Math.max(0, Math.min(100, percent));
  uploadBar.style.width = `${value}%`;
  uploadPct.textContent = `${Math.round(value)}%`;
  if (label) uploadStatusText.textContent = label;
}

function beginUpload(file) {
  const validation = validateFile(file);
  if (validation) {
    toast(validation, true);
    return;
  }

  const seq = ++uploadSeq;
  if (uploadXhr) {
    try { uploadXhr.abort(); } catch (_) { /* already finished */ }
  }
  uploaded = null;
  uploadError.hidden = true;
  uploadErrorText.textContent = "";
  dropzone.hidden = true;
  fileCard.hidden = false;
  fileCard.classList.remove("is-ready", "is-error");
  fileCard.classList.add("is-uploading");
  fileDetails.hidden = true;
  runSettings.hidden = true;
  uploadProgressWrap.hidden = false;
  uploadBar.style.width = "0%";
  uploadPct.textContent = "0%";
  uploadStatusText.textContent = "Uploading spreadsheet…";
  fileBadge.className = "soft-badge";
  fileBadge.textContent = "Uploading";
  $("fileIcon").textContent = fileIconLabel(file.name);
  $("fileName").textContent = file.name;
  $("fileMeta").textContent = `${fmtBytes(file.size)} · reading workbook`;
  updateHint();
  updateStartState();

  const form = new FormData();
  form.append("file", file);
  const xhr = new XMLHttpRequest();
  uploadXhr = xhr;
  const startedAt = performance.now();
  let lastPercent = 0;

  const update = (percent, label) => {
    if (seq !== uploadSeq) return;
    lastPercent = Math.max(lastPercent, percent);
    setUploadProgress(lastPercent, label);
  };

  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) {
      update(28, "Uploading spreadsheet…");
      return;
    }
    const pct = Math.min(86, Math.round((event.loaded / event.total) * 86));
    update(Math.max(8, pct), "Uploading spreadsheet…");
  });

  xhr.addEventListener("load", () => {
    if (seq !== uploadSeq) return;
    let payload = null;
    try { payload = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch (_) { payload = null; }
    if (xhr.status >= 200 && xhr.status < 300 && payload && payload.path) {
      update(100, "Reading workbook…");
      const wait = Math.max(0, 460 - (performance.now() - startedAt));
      window.setTimeout(() => {
        if (seq === uploadSeq) completeUpload(payload, file);
      }, wait);
    } else {
      failUpload(errorMessage(payload, "The server could not read this spreadsheet."), seq);
    }
  });

  xhr.addEventListener("error", () => {
    failUpload("Could not reach the local server. Is it still running?", seq);
  });

  xhr.addEventListener("abort", () => {
    if (seq === uploadSeq && uploadXhr === xhr) {
      uploadXhr = null;
    }
  });

  xhr.open("POST", "/api/upload");
  xhr.send(form);
  uploadXhr = xhr;
}

function completeUpload(data, file) {
  uploadXhr = null;
  uploaded = { ...data, size: file.size };
  fileCard.classList.remove("is-uploading", "is-error");
  fileCard.classList.add("is-ready");
  fileBadge.className = "soft-badge ready";
  fileBadge.textContent = "Ready";
  $("fileIcon").textContent = fileIconLabel(file.name);
  $("fileName").textContent = file.name;
  $("fileMeta").textContent = `${fmtBytes(file.size)} · ${data.sheet || "Sheet"}`;
  uploadProgressWrap.hidden = true;
  uploadError.hidden = true;
  uploadErrorText.textContent = "";
  renderFileDetails(data);
  fileDetails.hidden = false;
  runSettings.hidden = false;
  updateHint();
  updateStartState();
  renderWorkbook();
}

function failUpload(message, seq) {
  if (seq !== uploadSeq) return;
  uploadXhr = null;
  uploaded = null;
  fileCard.classList.remove("is-uploading", "is-ready");
  fileCard.classList.add("is-error");
  fileBadge.className = "soft-badge error";
  fileBadge.textContent = "Failed";
  uploadProgressWrap.hidden = true;
  uploadErrorText.textContent = message;
  uploadError.hidden = false;
  fileDetails.hidden = true;
  runSettings.hidden = true;
  updateStartState();
  updateHint();
  renderWorkbook();
  toast(message, true);
}

function renderFileDetails(data) {
  $("summaryRows").textContent = data.rows || 0;
  $("summaryDomains").textContent = data.unique_domains || 0;

  const duplicatesWrap = $("summaryDuplicatesWrap");
  if (data.duplicates) {
    duplicatesWrap.hidden = false;
    $("summaryDuplicates").textContent = data.duplicates;
  } else {
    duplicatesWrap.hidden = true;
  }

  const missingWrap = $("summaryMissingWrap");
  if (data.missing_domains) {
    missingWrap.hidden = false;
    $("summaryMissing").textContent = data.missing_domains;
  } else {
    missingWrap.hidden = true;
  }

  const chips = $("columnChips");
  chips.textContent = "";
  (data.columns || []).forEach((column) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = column;
    chips.appendChild(chip);
  });

  const body = $("previewBody");
  body.textContent = "";
  (data.preview || []).forEach((row) => {
    const tr = document.createElement("tr");
    [row.company_name, row.domain, row.team_size, row.niche].forEach((value) => {
      const td = document.createElement("td");
      td.textContent = value || "—";
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  $("previewWrap").hidden = !(data.preview || []).length;
  $("previewCaption").textContent = (data.preview || []).length
    ? `First ${data.preview.length} of ${data.rows} rows`
    : "No rows to preview";
}

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});

fileInput.addEventListener("change", (event) => {
  const file = event.target.files && event.target.files[0];
  if (file) beginUpload(file);
  event.target.value = "";
});

$("removeFileBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  resetUploadState();
});
$("replaceFileBtn").addEventListener("click", () => fileInput.click());
$("retryUploadBtn").addEventListener("click", () => fileInput.click());

let dragDepth = 0;
["dragenter", "dragover"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (name === "dragenter") dragDepth += 1;
    dropzone.classList.add("is-dragging");
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  });
});
dropzone.addEventListener("dragleave", (event) => {
  event.preventDefault();
  event.stopPropagation();
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dropzone.classList.remove("is-dragging");
});
dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  event.stopPropagation();
  dragDepth = 0;
  dropzone.classList.remove("is-dragging");
  const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
  if (file) beginUpload(file);
});

/* ---------------------------------------------------------------- workbook */
function renderWorkbook() {
  const empty = !uploaded;
  $("workbookEmpty").hidden = !empty;
  $("workbookFilled").hidden = empty;
  if (!uploaded) return;
  $("workbookName").textContent = uploaded.filename;
  $("workbookMeta").textContent = `${fmtBytes(uploaded.size)} · ${uploaded.sheet || "Sheet"}`;
  $("workbookIcon").textContent = fileIconLabel(uploaded.filename);
  $("workbookRows").textContent = uploaded.rows || 0;
  $("workbookDomains").textContent = uploaded.unique_domains || 0;
  $("workbookColumns").textContent = (uploaded.columns || []).length;
}

/* ---------------------------------------------------------------- settings */
function getLimit() {
  const value = parseInt(limitInput.value || "0", 10);
  if (Number.isNaN(value)) return 0;
  return Math.max(0, Math.min(50, value));
}

function updateHint() {
  const limit = getLimit();
  const hint = $("creditHint");
  const domains = uploaded ? Number(uploaded.unique_domains || 0) : 0;
  const maxCredits = Math.min(limit, domains);

  if (!uploaded) {
    $("footerNoteText").textContent = "Ledger protected · no file selected";
  } else if (!limit) {
    hint.textContent = "Choose how many new companies to process.";
    $("footerNoteText").textContent = "Choose companies · ledger protected";
  } else if (!domains) {
    hint.textContent = "This file has no usable company domains.";
    $("footerNoteText").textContent = "No usable domains · ledger protected";
  } else if (!maxCredits) {
    hint.textContent = "No new companies are selected for this limit.";
    $("footerNoteText").textContent = "No credits selected · ledger protected";
  } else {
    hint.textContent = `Live run: up to ${plural(maxCredits, "Apollo email credit")} may be used.`;
    $("footerNoteText").textContent = `Up to ${plural(maxCredits, "credit")} · one reveal per company`;
  }
  updateSetupLedger();
  updateStartState();
}

function updateSetupLedger() {
  const ledger = (current && current.ledger) || {};
  $("setupLedger").textContent = `Ledger: ${plural(ledger.companies || 0, "company")} · ${plural(ledger.credits_used || 0, "credit")} used · ${ledger.needs_review || 0} need review`;
}

function updateStartState() {
  const running = !!(current && current.running);
  const uploading = !!uploadXhr;
  const limit = getLimit();
  const hasDomains = !!(uploaded && uploaded.unique_domains);
  startBtn.disabled = running || uploading || !uploaded || !hasDomains || limit < 1;
  if (uploading) startBtn.textContent = "Uploading…";
  else if (running) startBtn.textContent = "Run in progress";
  else startBtn.textContent = "Start run";

  $("openSetupBtn").disabled = running;
  $("reviewBtn").disabled = running;
  $("chooseFileBtn").disabled = running;
}

limitInput.addEventListener("input", () => {
  const value = parseInt(limitInput.value || "0", 10);
  if (!Number.isNaN(value) && value > 50) limitInput.value = "50";
  updateHint();
});
limitInput.addEventListener("blur", () => {
  const value = parseInt(limitInput.value || "0", 10);
  if (Number.isNaN(value) || value < 1) limitInput.value = "1";
  if (value > 50) limitInput.value = "50";
  updateHint();
});
hideBrowser.addEventListener("change", updateHint);

/* ---------------------------------------------------------------- start */
startBtn.addEventListener("click", async () => {
  if (!uploaded) { toast("Upload a spreadsheet first.", true); return; }
  const limit = getLimit();
  if (limit < 1) { toast("Choose at least one new company to process.", true); return; }

  startBtn.disabled = true;
  startBtn.textContent = "Starting…";
  try {
    await api("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: uploaded.path,
        limit,
        dry_run: false,
        hide_browser: hideBrowser.checked,
      }),
    });
    hiddenLogRunId = null;
    closeModal();
    await poll();
    if (window.matchMedia("(max-width: 980px)").matches) {
      document.querySelector(".progress-card").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (error) {
    toast(error.message, true);
    updateStartState();
  }
});

stopBtn.addEventListener("click", async () => {
  stopBtn.disabled = true;
  stopBtn.textContent = "Stopping…";
  try {
    await api("/api/stop", { method: "POST" });
    toast("Stopping after the current company");
    await poll();
  } catch (error) {
    toast(error.message, true);
    stopBtn.disabled = false;
  } finally {
    stopBtn.textContent = "Stop run";
  }
});

/* ---------------------------------------------------------------- browser */
$("showBrowserBtn").addEventListener("click", async () => {
  try {
    await api("/api/show-browser", { method: "POST" });
    toast("Apollo browser restored");
    await poll();
  } catch (error) { toast(error.message, true); }
});

$("closeBrowserBtn").addEventListener("click", async () => {
  try {
    await api("/api/close-browser", { method: "POST" });
    toast("Apollo browser closed");
    await poll();
  } catch (error) { toast(error.message, true); }
});

$("openFolderBtn").addEventListener("click", async () => {
  try { await api("/api/reveal-output", { method: "POST" }); }
  catch (error) { toast(error.message, true); }
});

$("clearLogBtn").addEventListener("click", () => {
  hiddenLogRunId = current && current.run_id ? current.run_id : "hidden";
  logEl.textContent = "Log hidden for this view.";
});

/* ---------------------------------------------------------------- reset */
$("resetBtn").addEventListener("click", async () => {
  const domain = $("resetDomain").value.trim().toLowerCase();
  if (!domain) { toast("Enter the company domain first.", true); return; }
  const confirmed = window.confirm(
    `Reset ${domain} in the local ledger?\n\n` +
    "Only do this after checking Apollo manually. A later run will be allowed to reveal (and spend) one credit for this company again."
  );
  if (!confirmed) return;
  const button = $("resetBtn");
  button.disabled = true;
  try {
    const result = await api("/api/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ domain, confirm: "RESET" }),
    });
    toast(result.removed ? `Reset ${result.domain}` : `${result.domain} was not in the ledger`);
    $("resetDomain").value = "";
    await poll();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

/* ---------------------------------------------------------------- render */
function setBanner(text, kind = "info") {
  if (!text) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.className = "banner " + kind;
  banner.textContent = text;
}

function render(data) {
  current = data;
  const running = !!data.running;
  const phase = data.phase || "idle";
  const stopping = phase === "stopping";

  let pillClass = "idle";
  let pillText = "Idle";
  if (running) { pillClass = stopping ? "stopped" : "running"; pillText = stopping ? "Stopping" : "Running"; }
  else if (phase === "done") { pillClass = "done"; pillText = "Finished"; }
  else if (phase === "error") { pillClass = "error"; pillText = "Error"; }
  else if (phase === "stopped") { pillClass = "stopped"; pillText = "Stopped"; }
  statusPill.className = "pill " + pillClass;
  statusPillText.textContent = pillText;

  phaseChip.className = "phase-chip " + (running ? (stopping ? "stopped" : "running") : phase === "done" ? "done" : phase === "error" ? "error" : phase === "stopped" ? "stopped" : "");
  phaseChip.textContent = running ? (data.status_text || "Running") : phase === "idle" ? "Idle" : (data.status_text || "Idle");

  const planned = Number(data.planned || 0);
  const handled = Number(data.handled || 0);
  const pct = planned > 0 ? Math.min(100, Math.round((handled / planned) * 100)) : (phase === "done" && data.output_path ? 100 : 0);
  $("progressBar").style.width = pct + "%";
  $("progressCount").textContent = `${handled} / ${planned || 0} companies`;
  $("runMeta").textContent = data.run_id ? `run ${data.run_id} · ${fmtElapsed(data.started_at)}` : "No active run";

  $("progressTitle").textContent = running
    ? (data.status_text || "Running")
    : (phase === "done" ? "Run finished" : data.status_text || "Ready when you are");
  if (data.current_company) {
    $("currentCompany").textContent = `${data.current_company}${data.current_domain ? " · " + data.current_domain : ""}`;
  } else {
    $("currentCompany").textContent = running
      ? "Preparing…"
      : (data.output_path ? "Open the output below." : uploaded ? "Workbook ready. Start a run when you're ready." : "Upload a file to begin.");
  }
  if (data.current_person) {
    $("personLine").hidden = false;
    $("personLine").textContent = "On: " + data.current_person;
  } else {
    $("personLine").hidden = true;
  }

  $("statHandled").textContent = handled;
  $("statCredits").textContent = data.credits || 0;
  $("statVerified").textContent = data.verified || 0;
  $("statGuessed").textContent = data.guessed || 0;
  $("statMasked").textContent = data.masked || 0;
  $("statSkipped").textContent = `${data.skipped || 0} / ${data.errors || 0}`;

  let message = "";
  let kind = "info";
  if (data.challenge) {
    message = "Apollo showed a verification/challenge screen. The run stopped and the Chrome window was restored so you can solve it, then start again.";
    kind = "error";
  } else if (data.login_required) {
    message = "Apollo needs a login. The Chrome window was restored; log in there, then start again.";
    kind = "info";
  } else if (!running && data.last_error) {
    message = data.last_error;
    kind = "error";
  }
  setBanner(message, kind);

  stopBtn.hidden = !running;
  stopBtn.disabled = !running || stopping;
  stopBtn.textContent = stopping ? "Stopping…" : "Stop run";
  $("resetBtn").disabled = running;
  $("showBrowserBtn").hidden = !data.browser_open;
  $("closeBrowserBtn").hidden = !(data.browser_open && !running);

  if (data.output_path) {
    $("outputCard").hidden = false;
    $("outputName").textContent = baseName(data.output_path);
  } else {
    $("outputCard").hidden = true;
  }

  const ledger = data.ledger || {};
  $("ledgerText").textContent = `${plural(ledger.companies || 0, "company")} · ${plural(ledger.credits_used || 0, "credit")} · ${ledger.needs_review || 0} need review`;
  updateSetupLedger();
  updateStartState();
  if (running && !setupModal.hidden) closeModal();

  renderLogs(data);
  renderHistory(data.recent_runs || []);
  maybeOpenInitialModal(data);
}

function maybeOpenInitialModal(data) {
  if (initialModalHandled) return;
  initialModalHandled = true;
  if (data.running) return;
  window.setTimeout(() => {
    if (setupModal.hidden && !(current && current.running) && !uploaded) openModal();
  }, 320);
}

function renderLogs(data) {
  if (hiddenLogRunId && hiddenLogRunId === data.run_id) return;
  if (hiddenLogRunId && hiddenLogRunId !== data.run_id) hiddenLogRunId = null;
  const lines = data.logs || [];
  const text = lines.length ? lines.join("\n") : "Waiting for a run…";
  const nearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  logEl.textContent = text;
  if (nearBottom) logEl.scrollTop = logEl.scrollHeight;
}

function renderHistory(runs) {
  const body = $("historyBody");
  body.textContent = "";
  if (!runs.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "empty";
    td.textContent = "No runs yet.";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  runs.forEach((run) => {
    const tr = document.createElement("tr");
    const started = run.started_at
      ? new Date(run.started_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
      : "—";
    const cells = [
      started,
      run.status || "—",
      run.companies_handled ?? "—",
      run.credits_spent ?? "—",
      run.verified ?? "—",
      run.guessed ?? "—",
      baseName(run.output_path) || "—",
    ];
    cells.forEach((value, index) => {
      const td = document.createElement("td");
      if (index === 1) {
        const span = document.createElement("span");
        span.className = "run-status " + (run.status || "");
        span.textContent = value;
        td.appendChild(span);
      } else {
        td.textContent = value;
      }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
}

/* ----------------------------------------------------------------- poll */
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const data = await api("/api/status");
    render(data);
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, data.running ? 1000 : 2500);
  } catch (error) {
    setBanner("Cannot reach the local server: " + error.message, "error");
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, 3000);
  } finally {
    polling = false;
  }
}

(async function init() {
  renderWorkbook();
  updateHint();
  await poll();
})();
