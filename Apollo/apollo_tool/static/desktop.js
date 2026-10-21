const $ = (id) => document.getElementById(id);

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
const ALLOWED_EXTENSIONS = ["xlsx", "xlsm", "csv", "json"];

const state = {
  uploaded: null,
  status: null,
  limit: 0,
  uploadXhr: null,
  uploadSeq: 0,
  polling: false,
  pollTimer: null,
  showResult: false,
  dismissedUploadKey: "",
  toastHideTimer: null,
  toastRemoveTimer: null,
};

const statusPill = $("statusPill");
const statusPillText = $("statusPillText");
const uploadView = $("uploadView");
const runView = $("runView");
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
const idleHint = $("idleHint");
const startBtn = $("startBtn");
const limitPicker = $("limitPicker");

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { detail: text }; }
  if (!response.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((item) => item.msg || String(item)).join(", ");
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return data;
}

function toast(message, isError = false) {
  const el = $("toast");
  clearTimeout(state.toastHideTimer);
  clearTimeout(state.toastRemoveTimer);
  el.textContent = message;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  void el.offsetWidth;
  requestAnimationFrame(() => el.classList.add("show"));
  state.toastHideTimer = setTimeout(() => {
    el.classList.remove("show");
    state.toastRemoveTimer = setTimeout(() => { el.hidden = true; }, 180);
  }, 3800);
}

function fmtBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function baseName(path) {
  if (!path) return "";
  return String(path).split(/[\\/]/).pop();
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

function fmtElapsed(startedAt, finishedAt) {
  if (!startedAt) return "0:00";
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return "0:00";
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  if (Number.isNaN(end)) return "0:00";
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

function uploadIdentity(upload) {
  if (!upload) return "";
  return [upload.uploaded_at, upload.filename, upload.size].join("|");
}

function uploadKey(upload) {
  if (!upload) return "";
  return uploadIdentity(upload) + "|" + (upload.status || "");
}

function limitLabel(limit) {
  return `${limit} ${limit === 1 ? "company" : "companies"}`;
}

/* ---------------------------------------------------------------- views */
function showUploadView() {
  uploadView.hidden = false;
  runView.hidden = true;
}

function showRunView() {
  uploadView.hidden = true;
  runView.hidden = false;
}

function setPill(kind, text) {
  statusPill.className = "pill " + kind;
  statusPillText.textContent = text;
}

function setLimit(limit) {
  const value = Number(limit) || 0;
  state.limit = value > 0 ? Math.min(25, value) : 0;
  [...limitPicker.querySelectorAll("[data-limit]")].forEach((button) => {
    button.classList.toggle("is-active", Number(button.dataset.limit) === state.limit);
  });
  const domains = state.uploaded ? Number(state.uploaded.unique_domains || 0) : 0;
  if (!state.limit) {
    $("creditHint").textContent = "Choose a credit limit before starting.";
  } else {
    $("creditHint").textContent = domains
      ? `Up to ${Math.min(state.limit, domains)} Apollo email credits may be used.`
      : "Up to " + limitLabel(state.limit) + " may be processed.";
  }
  updateStartState();
}

function updateStartState() {
  const running = !!(state.status && state.status.running);
  const uploading = !!state.uploadXhr;
  const domains = state.uploaded ? Number(state.uploaded.unique_domains || 0) : 0;
  startBtn.disabled = running || uploading || !state.uploaded || !domains || !state.limit;
  if (uploading) $("startBtnText").textContent = "Uploading…";
  else if (running) $("startBtnText").textContent = "Run in progress";
  else if (!state.limit) $("startBtnText").textContent = "Choose a limit";
  else $("startBtnText").textContent = "Start run";
  $("removeFileBtn").disabled = running || uploading;
  $("replaceFileBtn").disabled = running || uploading;
}

/* ---------------------------------------------------------------- upload */
function validateFile(file) {
  if (!file) return "Choose a spreadsheet first.";
  if (!ALLOWED_EXTENSIONS.includes(extensionOf(file.name))) {
    return "Use an .xlsx, .xlsm, .csv or .json file.";
  }
  if (!file.size) return "That file is empty.";
  if (file.size > MAX_UPLOAD_BYTES) return "That file is larger than 25 MB.";
  return "";
}

function resetUploadState(dismissCurrent = false) {
  state.uploadSeq += 1;
  if (state.uploadXhr) {
    try { state.uploadXhr.abort(); } catch (_) { /* already finished */ }
    state.uploadXhr = null;
  }
  if (dismissCurrent && state.uploaded) {
    state.dismissedUploadKey = uploadKey(state.uploaded);
  }
  state.uploaded = null;
  state.limit = 0;
  fileInput.value = "";
  dropzone.hidden = false;
  fileCard.hidden = true;
  fileCard.classList.remove("is-uploading", "is-ready", "is-error");
  uploadProgressWrap.hidden = false;
  uploadBar.style.width = "0%";
  uploadPct.textContent = "0%";
  uploadStatusText.textContent = "Uploading spreadsheet…";
  uploadError.hidden = true;
  uploadErrorText.textContent = "";
  fileDetails.hidden = true;
  idleHint.hidden = false;
  updateStartState();
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
  const seq = ++state.uploadSeq;
  if (state.uploadXhr) {
    try { state.uploadXhr.abort(); } catch (_) { /* already finished */ }
  }
  state.uploaded = null;
  state.limit = 0;
  state.dismissedUploadKey = "";
  uploadError.hidden = true;
  uploadErrorText.textContent = "";
  dropzone.hidden = true;
  fileCard.hidden = false;
  fileCard.classList.remove("is-ready", "is-error");
  fileCard.classList.add("is-uploading");
  fileDetails.hidden = true;
  idleHint.hidden = true;
  uploadProgressWrap.hidden = false;
  uploadBar.style.width = "0%";
  uploadPct.textContent = "0%";
  uploadStatusText.textContent = "Uploading spreadsheet…";
  fileBadge.className = "soft-badge";
  fileBadge.textContent = "Uploading";
  $("fileIcon").textContent = fileIconLabel(file.name);
  $("fileName").textContent = file.name;
  $("fileMeta").textContent = `${fmtBytes(file.size)} · reading workbook`;
  updateStartState();

  const form = new FormData();
  form.append("file", file);
  form.append("source", "desktop");
  const xhr = new XMLHttpRequest();
  state.uploadXhr = xhr;
  const startedAt = performance.now();
  let lastPercent = 0;

  const update = (percent, label) => {
    if (seq !== state.uploadSeq) return;
    lastPercent = Math.max(lastPercent, percent);
    setUploadProgress(lastPercent, label);
  };

  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) {
      update(24, "Uploading spreadsheet…");
      return;
    }
    const pct = Math.min(86, Math.round((event.loaded / event.total) * 86));
    update(Math.max(8, pct), "Uploading spreadsheet…");
  });

  xhr.addEventListener("load", () => {
    if (seq !== state.uploadSeq) return;
    let payload = null;
    try { payload = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch (_) { payload = null; }
    if (xhr.status >= 200 && xhr.status < 300 && payload && payload.path) {
      update(100, "Reading workbook…");
      const wait = Math.max(0, 850 - (performance.now() - startedAt));
      setTimeout(() => {
        if (seq === state.uploadSeq) completeUpload(payload);
      }, wait);
    } else {
      let detail = payload && payload.detail;
      if (Array.isArray(detail)) detail = detail.map((item) => item.msg || String(item)).join(", ");
      failUpload(detail || "The local app could not read this spreadsheet.", seq);
    }
  });

  xhr.addEventListener("error", () => {
    failUpload("Could not reach the local app. Is it still running?", seq);
  });

  xhr.open("POST", "/api/upload");
  xhr.send(form);
  state.uploadXhr = xhr;
}

function completeUpload(data) {
  state.uploadXhr = null;
  state.limit = 0;
  state.uploaded = { ...data, size: Number(data.size || 0) };
  state.dismissedUploadKey = "";
  renderReadyFile(state.uploaded);
  toast("File uploaded");
}

function failUpload(message, seq) {
  if (seq !== state.uploadSeq) return;
  state.uploadXhr = null;
  state.uploaded = null;
  fileCard.classList.remove("is-uploading", "is-ready");
  fileCard.classList.add("is-error");
  fileBadge.className = "soft-badge error";
  fileBadge.textContent = "Failed";
  uploadProgressWrap.hidden = true;
  uploadErrorText.textContent = message;
  uploadError.hidden = false;
  fileDetails.hidden = true;
  idleHint.hidden = true;
  updateStartState();
  toast(message, true);
}

function renderReadyFile(data) {
  fileCard.hidden = false;
  dropzone.hidden = true;
  fileCard.classList.remove("is-uploading", "is-error");
  fileCard.classList.add("is-ready");
  fileBadge.className = "soft-badge ready";
  fileBadge.textContent = "Ready";
  $("fileIcon").textContent = fileIconLabel(data.filename || data.path);
  $("fileName").textContent = data.filename || baseName(data.path);
  $("fileMeta").textContent = `${fmtBytes(data.size)} · ${data.sheet || "Sheet"}`;
  uploadProgressWrap.hidden = true;
  uploadError.hidden = true;
  uploadErrorText.textContent = "";
  $("summaryRows").textContent = data.rows || 0;
  $("summaryDomains").textContent = data.unique_domains || 0;
  $("summaryColumns").textContent = (data.columns || []).length;
  fileDetails.hidden = false;
  idleHint.hidden = true;
  setLimit(state.limit);
  updateStartState();
}

function renderUploading(data) {
  if (!data) return;
  dropzone.hidden = true;
  fileCard.hidden = false;
  fileCard.classList.remove("is-ready", "is-error");
  fileCard.classList.add("is-uploading");
  fileBadge.className = "soft-badge";
  fileBadge.textContent = "Uploading";
  $("fileIcon").textContent = fileIconLabel(data.filename);
  $("fileName").textContent = data.filename || "Uploading file…";
  $("fileMeta").textContent = [fmtBytes(data.size), "reading workbook"].filter(Boolean).join(" · ");
  uploadProgressWrap.hidden = false;
  uploadBar.style.width = "45%";
  uploadPct.textContent = "…";
  uploadStatusText.textContent = data.source === "telegram" ? "Receiving file from Telegram…" : "Uploading spreadsheet…";
  fileDetails.hidden = true;
  idleHint.hidden = true;
  updateStartState();
}

/* ---------------------------------------------------------------- status */
function applyLastUpload(upload) {
  if (!upload) return;
  const identity = uploadIdentity(upload);
  const key = uploadKey(upload);
  if (key === state.dismissedUploadKey) return;
  if (upload.status === "uploading") {
    if (!state.uploadXhr) renderUploading(upload);
    return;
  }
  if (upload.status === "ready") {
    if (state.uploadXhr) return;
    if (state.uploaded && uploadIdentity(state.uploaded) === identity) return;
    state.uploaded = upload;
    state.limit = 0;
    renderReadyFile(upload);
    return;
  }
  if (upload.status === "error" && !state.uploadXhr) {
    dropzone.hidden = true;
    fileCard.hidden = false;
    fileCard.classList.remove("is-uploading", "is-ready");
    fileCard.classList.add("is-error");
    fileBadge.className = "soft-badge error";
    fileBadge.textContent = "Failed";
    $("fileName").textContent = upload.filename || "File";
    $("fileMeta").textContent = "Upload failed";
    uploadProgressWrap.hidden = true;
    uploadErrorText.textContent = upload.error || "The file could not be read.";
    uploadError.hidden = false;
    fileDetails.hidden = true;
    idleHint.hidden = true;
  }
}

function setRunPill(data) {
  const running = !!data.running;
  const phase = data.phase || "idle";
  if (running) {
    setPill("running", phase === "stopping" ? "Stopping" : "Running");
  } else if (phase === "done") {
    setPill("done", "Finished");
  } else if (phase === "error") {
    setPill("error", "Error");
  } else if (phase === "stopped") {
    setPill("stopped", "Stopped");
  } else {
    setPill("idle", "Idle");
  }
}

function renderRun(data) {
  const running = !!data.running;
  const phase = data.phase || "idle";
  const planned = Number(data.planned || 0);
  const handled = Number(data.handled || 0);
  const pct = planned > 0
    ? Math.min(100, Math.round((handled / planned) * 100))
    : (!running && data.output_path ? 100 : 0);
  const done = !running && (phase === "done" || phase === "stopped" || phase === "error");

  $("progressBar").style.width = pct + "%";
  $("progressCount").textContent = `${handled} / ${planned || 0} companies`;
  $("runElapsed").textContent = fmtElapsed(data.started_at, done ? data.finished_at : null);
  $("statHandled").textContent = handled;
  $("statCredits").textContent = data.credits || 0;
  $("statVerified").textContent = data.verified || 0;
  $("statGuessed").textContent = data.guessed || 0;

  const title = running
    ? (phase === "stopping" ? "Stopping after this company" : "Processing companies")
    : phase === "done" ? "Run complete" : phase === "error" ? "Run needs attention" : "Run stopped";
  $("runTitle").textContent = title;
  $("phaseChip").textContent = running ? "Live" : (data.status_text || (phase === "done" ? "Finished" : phase));
  $("phaseChip").className = "phase-chip " + (running ? "running" : phase === "done" ? "done" : phase === "error" ? "error" : "stopped");

  if (data.current_company) {
    $("currentCompany").textContent = `${data.current_company}${data.current_domain ? " · " + data.current_domain : ""}`;
  } else {
    $("currentCompany").textContent = running ? "Preparing…" : (data.output_path ? "Workbook ready." : "No active run.");
  }

  const logs = data.logs || [];
  const last = logs.length ? String(logs[logs.length - 1]) : "";
  $("activityLine").textContent = last || (running ? "Waiting for the first update…" : "Ready.");
  $("activityLine").title = last;

  const challenge = !!data.challenge;
  const login = !!data.login_required;
  $("challengeBox").hidden = !(challenge || login || (!running && data.last_error));
  if (!$("challengeBox").hidden) {
    $("challengeTitle").textContent = challenge ? "Apollo challenge" : login ? "Apollo login needed" : "Run error";
    $("challengeText").textContent = challenge
      ? "Chrome was brought to the front. Solve the challenge, then start again."
      : login
        ? "Log in in the Chrome window, then start again."
        : data.last_error || "The run stopped.";
  }

  $("stopBtn").hidden = !running;
  $("stopBtn").disabled = !running || phase === "stopping";
  $("stopBtn").textContent = phase === "stopping" ? "Stopping…" : "Stop run";
  $("showBrowserBtn").hidden = !data.browser_open || running;
  $("showBrowserBtn").disabled = !data.browser_open;
  $("newRunBtn").hidden = running;

  const outputPath = data.output_path && !running ? data.output_path : "";
  $("outputCard").hidden = !outputPath;
  if (outputPath) $("outputName").textContent = baseName(outputPath);
  updateStartState();
}

function renderStatus(data) {
  const wasRunning = !!(state.status && state.status.running);
  state.status = data;
  setRunPill(data);
  if (
    wasRunning &&
    !data.running &&
    ["done", "stopped", "error"].includes(String(data.phase || ""))
  ) {
    state.showResult = true;
  }

  if (data.running) {
    state.showResult = false;
    showRunView();
    renderRun(data);
    return;
  }

  const upload = data.last_upload;
  const incomingKey = uploadKey(upload);
  if (
    upload &&
    incomingKey !== state.dismissedUploadKey &&
    (!state.uploaded || incomingKey !== uploadKey(state.uploaded))
  ) {
    // A new file arrived from Telegram (or another local window).  Leave the
    // completed-run card and show the new upload instead of hiding it.
    state.showResult = false;
  }

  if (state.showResult) {
    showRunView();
    renderRun(data);
    return;
  }

  if (upload && (upload.status === "uploading" || upload.status === "error")) {
    showUploadView();
    applyLastUpload(upload);
    return;
  }
  if (upload && upload.status === "ready") {
    showUploadView();
    applyLastUpload(upload);
    return;
  }

  showUploadView();
  renderRun(data);
  updateStartState();
}

/* ---------------------------------------------------------------- actions */
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

["dragenter", "dragover"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    event.stopPropagation();
    dropzone.classList.add("is-dragging");
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  });
});
dropzone.addEventListener("dragleave", (event) => {
  event.preventDefault();
  event.stopPropagation();
  dropzone.classList.remove("is-dragging");
});
dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  event.stopPropagation();
  dropzone.classList.remove("is-dragging");
  const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
  if (file) beginUpload(file);
});

$("removeFileBtn").addEventListener("click", async (event) => {
  event.stopPropagation();
  if (state.status && state.status.running) return;
  resetUploadState(true);
  try {
    await api("/api/dismiss-upload", { method: "POST" });
  } catch (error) {
    // The local file card is already gone; a stale server preview will be
    // ignored by the upload key on the next poll.
    console.warn(error.message);
  }
});
$("replaceFileBtn").addEventListener("click", () => fileInput.click());
$("retryUploadBtn").addEventListener("click", () => fileInput.click());

limitPicker.addEventListener("click", (event) => {
  const button = event.target.closest("[data-limit]");
  if (!button) return;
  setLimit(Number(button.dataset.limit));
});

startBtn.addEventListener("click", async () => {
  if (!state.uploaded) {
    toast("Upload a spreadsheet first.", true);
    return;
  }
  if (!state.limit) {
    toast("Choose a credit limit first.", true);
    return;
  }
  if (!state.uploaded.unique_domains) {
    toast("This file has no usable company domains.", true);
    return;
  }
  startBtn.disabled = true;
  $("startBtnText").textContent = "Starting…";
  try {
    await api("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: state.uploaded.path,
        limit: state.limit,
        dry_run: false,
        hide_browser: true,
      }),
    });
    state.showResult = false;
    showRunView();
    await poll();
  } catch (error) {
    toast(error.message, true);
  } finally {
    updateStartState();
  }
});

$("stopBtn").addEventListener("click", async () => {
  const button = $("stopBtn");
  button.disabled = true;
  button.textContent = "Stopping…";
  try {
    await api("/api/stop", { method: "POST" });
    toast("Stopping after the current company");
    await poll();
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
});

$("showBrowserBtn").addEventListener("click", async () => {
  try {
    await api("/api/show-browser", { method: "POST" });
    toast("Chrome window restored");
    await poll();
  } catch (error) {
    toast(error.message, true);
  }
});

$("openFolderBtn").addEventListener("click", async () => {
  try {
    await api("/api/reveal-output", { method: "POST" });
  } catch (error) {
    toast(error.message, true);
  }
});

$("newRunBtn").addEventListener("click", () => {
  state.showResult = false;
  state.limit = 0;
  showUploadView();
  if (state.uploaded) renderReadyFile(state.uploaded);
  else resetUploadState(false);
  poll();
});

/* ---------------------------------------------------------------- poll */
async function poll() {
  if (state.polling) return;
  state.polling = true;
  try {
    const data = await api("/api/status");
    renderStatus(data);
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(poll, data.running ? 600 : 700);
  } catch (error) {
    setPill("error", "Offline");
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(poll, 1600);
  } finally {
    state.polling = false;
  }
}

/* ---------------------------------------------------------------- init */
(function init() {
  setLimit(0);
  showUploadView();
  resetUploadState(false);
  poll();
})();
