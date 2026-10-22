const $ = (id) => document.getElementById(id);

const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ALLOWED_EXTENSIONS = ["xlsx", "xlsm", "csv", "json"];
const CLOSE_DELAY_MS = 780;

const state = {
  uploaded: null,
  status: null,
  limit: 0,
  uploadXhr: null,
  uploadSeq: 0,
  polling: false,
  pollTimer: null,
  session: null,
  lastRunning: null,
  startedHere: false,
  closing: false,
  toastHideTimer: null,
  toastRemoveTimer: null,
};

const uploadCard = $("uploadCard");
const uploadState = $("uploadState");
const runState = $("runState");
const dropzone = $("dropzone");
const fileInput = $("fileInput");
const fileCard = $("fileCard");
const fileDetails = $("fileDetails");
const uploadError = $("uploadError");
const uploadErrorText = $("uploadErrorText");
const errorTitle = $("errorTitle");
const idleHint = $("idleHint");
const removeTaskBtn = $("removeTaskBtn");
const startBtn = $("startBtn");
const startBtnText = $("startBtnText");
const startOverlay = $("startOverlay");
const limitPicker = $("limitPicker");
const uploadBar = $("uploadBar");
const creditHint = $("creditHint");

/* ------------------------------------------------------------------ helpers */
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
    state.toastRemoveTimer = setTimeout(() => { el.hidden = true; }, 200);
  }, 3400);
}

function fmtBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function extensionOf(name) {
  const dot = (name || "").lastIndexOf(".");
  return dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
}

function extensionLabel(name) {
  const extension = extensionOf(name);
  return extension ? `.${extension}` : "file";
}

function baseName(path) {
  if (!path) return "";
  return String(path).split(/[\\/]/).pop();
}

function validateFile(file) {
  if (!file) return "Choose a spreadsheet first.";
  if (!ALLOWED_EXTENSIONS.includes(extensionOf(file.name))) {
    return "Use an .xlsx, .xlsm, .csv or .json file.";
  }
  if (!file.size) return "That file is empty.";
  if (file.size > MAX_UPLOAD_BYTES) return "That file is larger than 10 MB.";
  return "";
}

function setLimit(limit) {
  const value = Number(limit) || 0;
  state.limit = value > 0 ? Math.min(25, value) : 0;
  [...limitPicker.querySelectorAll("[data-limit]")].forEach((button) => {
    button.classList.toggle("is-active", Number(button.dataset.limit) === state.limit);
  });
  const domains = state.uploaded ? Number(state.uploaded.unique_domains || 0) : 0;
  if (!state.limit) {
    creditHint.textContent = "Select a limit";
  } else {
    creditHint.textContent = domains
      ? `Up to ${Math.min(state.limit, domains)} this run`
      : "Ready to start";
  }
  updateStartState();
}

function updateStartState() {
  const running = !!(state.status && state.status.running);
  const uploading = !!state.uploadXhr;
  const domains = state.uploaded ? Number(state.uploaded.unique_domains || 0) : 0;
  startBtn.disabled = running || uploading || !state.uploaded || !domains || !state.limit;
  if (uploading) startBtnText.textContent = "Uploading…";
  else if (running) startBtnText.textContent = "Run in progress";
  else if (!state.uploaded) startBtnText.textContent = "Upload a file";
  else if (!state.limit) startBtnText.textContent = "Choose a limit";
  else startBtnText.textContent = "Start run";
  $("removeFileBtn").disabled = running || uploading;
  $("chooseAnotherBtn").disabled = running || uploading;
  removeTaskBtn.disabled = running || uploading;
}

/* ------------------------------------------------------------------ views */
function showUploadView() {
  uploadState.hidden = false;
  runState.hidden = true;
}

function showRunView() {
  uploadState.hidden = true;
  runState.hidden = false;
}

function showStartOverlay() {
  // Keep the upload view mounted underneath: it gives the card its height, so
  // the overlay stays centered instead of collapsing to the card padding.
  startOverlay.hidden = false;
  runState.hidden = true;
}

function resetUploadState() {
  state.uploadSeq += 1;
  if (state.uploadXhr) {
    try { state.uploadXhr.abort(); } catch (_) { /* already finished */ }
    state.uploadXhr = null;
  }
  state.uploaded = null;
  state.limit = 0;
  fileInput.value = "";
  dropzone.hidden = false;
  dropzone.classList.remove("is-dragging");
  fileCard.hidden = true;
  fileCard.classList.remove("is-uploading", "is-ready", "is-error");
  fileCard.style.animation = "";
  uploadBar.style.width = "0%";
  fileDetails.hidden = true;
  removeTaskBtn.hidden = true;
  uploadError.hidden = true;
  uploadErrorText.textContent = "";
  idleHint.hidden = false;
  [...limitPicker.querySelectorAll("[data-limit]")].forEach((button) => {
    button.classList.remove("is-active");
  });
  creditHint.textContent = "Select a limit";
  updateStartState();
}

/* ------------------------------------------------------------------ upload */
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
  uploadError.hidden = true;
  uploadErrorText.textContent = "";

  dropzone.hidden = true;
  fileCard.hidden = false;
  fileCard.classList.remove("is-ready", "is-error");
  fileCard.classList.add("is-uploading");
  fileDetails.hidden = true;
  removeTaskBtn.hidden = false;
  idleHint.hidden = true;
  $("fileName").textContent = file.name;
  $("fileMeta").textContent = `Uploading… ${fmtBytes(file.size)}`;
  uploadBar.style.width = "0%";
  updateStartState();

  const form = new FormData();
  form.append("file", file);
  form.append("source", "desktop");

  const xhr = new XMLHttpRequest();
  state.uploadXhr = xhr;
  const startedAt = performance.now();

  xhr.upload.addEventListener("progress", (event) => {
    if (seq !== state.uploadSeq) return;
    const percent = event.lengthComputable
      ? Math.min(86, Math.round((event.loaded / event.total) * 86))
      : 26;
    uploadBar.style.width = `${Math.max(6, percent)}%`;
    $("fileMeta").textContent = `Uploading… ${Math.max(6, percent)}%`;
  });

  xhr.addEventListener("load", () => {
    if (seq !== state.uploadSeq) return;
    let payload = null;
    try { payload = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch (_) { payload = null; }
    if (xhr.status >= 200 && xhr.status < 300 && payload && payload.path) {
      uploadBar.style.width = "100%";
      $("fileMeta").textContent = "Reading workbook…";
      const wait = Math.max(0, 520 - (performance.now() - startedAt));
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

  xhr.addEventListener("abort", () => {
    if (seq === state.uploadSeq) state.uploadXhr = null;
  });

  xhr.open("POST", "/api/upload");
  xhr.send(form);
}

function completeUpload(data) {
  state.uploadXhr = null;
  state.uploaded = { ...data, size: Number(data.size || 0) };
  state.limit = 0;

  fileCard.hidden = false;
  fileCard.classList.remove("is-uploading", "is-error");
  fileCard.classList.add("is-ready");
  dropzone.hidden = true;
  $("fileName").textContent = data.filename || baseName(data.path) || "Spreadsheet";
  $("fileMeta").textContent = `${extensionLabel(data.filename || data.path)} | ${fmtBytes(data.size)}`;
  uploadError.hidden = true;
  uploadErrorText.textContent = "";
  fileDetails.hidden = false;
  removeTaskBtn.hidden = false;
  idleHint.hidden = true;
  [...limitPicker.querySelectorAll("[data-limit]")].forEach((button) => {
    button.classList.remove("is-active");
  });
  creditHint.textContent = "Select a limit";
  updateStartState();
}

function failUpload(message, seq) {
  if (seq !== state.uploadSeq) return;
  state.uploadXhr = null;
  state.uploaded = null;
  fileCard.hidden = true;
  fileCard.classList.remove("is-uploading", "is-ready");
  dropzone.hidden = false;
  fileDetails.hidden = true;
  removeTaskBtn.hidden = true;
  idleHint.hidden = false;
  errorTitle.textContent = "Could not read this file";
  uploadErrorText.textContent = message;
  uploadError.hidden = false;
  updateStartState();
  toast(message, true);
}

async function removeFile() {
  if (state.status && state.status.running) return;
  resetUploadState();
  try {
    await api("/api/dismiss-upload", { method: "POST" });
  } catch (error) {
    console.warn(error.message);
  }
}

/* ------------------------------------------------------------------ status */
function fmtElapsed(startedAt, finishedAt) {
  if (!startedAt) return "0:00";
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return "0:00";
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function renderRun(data) {
  const running = !!data.running;
  const planned = Number(data.planned || 0);
  const handled = Number(data.handled || 0);
  const pct = planned > 0
    ? Math.min(100, Math.round((handled / planned) * 100))
    : (!running && data.output_path ? 100 : 0);

  $("runPct").textContent = `${pct}%`;
  $("runBar").style.width = `${pct}%`;
  $("runTitle").textContent = running ? "Apollo is working" : "Run finished";
  $("currentCompany").textContent = data.current_company
    ? `${data.current_company}${data.current_domain ? " · " + data.current_domain : ""}`
    : running ? "Preparing Chrome…" : "Workbook saved.";
  $("statHandled").textContent = handled;
  $("statVerified").textContent = data.verified || 0;
  $("statGuessed").textContent = data.guessed || 0;
  $("statCredits").textContent = data.credits || 0;
  $("runTitle").title = fmtElapsed(data.started_at, running ? null : data.finished_at);

  const errorLine = data.last_error || data.challenge || "";
  if (errorLine && !running) {
    $("currentCompany").textContent = String(errorLine).slice(0, 120);
  }
}

function renderStatus(data) {
  state.status = data;
  const session = Number(data.desktop_session ?? 0);

  if (state.session === null) {
    state.session = session;
  } else if (session !== state.session) {
    state.session = session;
    if (!state.startedHere && !data.running) {
      resetUploadState();
      showUploadView();
      state.lastRunning = false;
    }
  }

  if (state.startedHere || state.closing) {
    showStartOverlay();
    state.lastRunning = !!data.running;
    return;
  }

  if (data.running) {
    const justStarted = state.lastRunning === false;
    state.lastRunning = true;
    if (justStarted) {
      // A run was started from another surface while this uploader was open.
      // Close the uploader exactly like a start from this window.
      state.startedHere = true;
      showStartOverlay();
      scheduleClose();
      return;
    }
    showRunView();
    renderRun(data);
    return;
  }

  state.lastRunning = false;
  showUploadView();
  updateStartState();
}

/* ------------------------------------------------------------------ start + close */
function scheduleClose() {
  if (state.closing) return;
  state.closing = true;
  clearTimeout(state.pollTimer);
  setTimeout(() => {
    uploadCard.classList.add("is-closing");
    fetch("/api/desktop/close", { method: "POST", keepalive: true }).catch(() => {});
    setTimeout(() => {
      try { window.close(); } catch (_) { /* app mode is closed by the local app */ }
    }, 480);
  }, CLOSE_DELAY_MS);
}

async function startRun() {
  if (!state.uploaded) {
    toast("Upload a spreadsheet first.", true);
    return;
  }
  if (!state.limit) {
    toast("Choose a limit first.", true);
    return;
  }
  if (!state.uploaded.unique_domains) {
    toast("This file has no usable company domains.", true);
    return;
  }

  startBtn.disabled = true;
  startBtnText.textContent = "Starting…";

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
    state.startedHere = true;
    showStartOverlay();
    scheduleClose();
  } catch (error) {
    toast(error.message, true);
    startBtn.disabled = false;
    updateStartState();
  }
}

/* ------------------------------------------------------------------ events */
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

["dragover", "drop"].forEach((name) => {
  window.addEventListener(name, (event) => event.preventDefault());
});

$("removeFileBtn").addEventListener("click", removeFile);
$("removeTaskBtn").addEventListener("click", removeFile);
$("chooseAnotherBtn").addEventListener("click", () => fileInput.click());
$("retryUploadBtn").addEventListener("click", () => fileInput.click());

limitPicker.addEventListener("click", (event) => {
  const button = event.target.closest("[data-limit]");
  if (!button) return;
  setLimit(Number(button.dataset.limit));
});

startBtn.addEventListener("click", startRun);

/* ------------------------------------------------------------------ poll */
async function poll() {
  if (state.polling || state.closing) return;
  state.polling = true;
  try {
    const data = await api("/api/status");
    renderStatus(data);
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(poll, data.running ? 650 : 850);
  } catch (error) {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(poll, 1600);
  } finally {
    state.polling = false;
  }
}

/* ------------------------------------------------------------------ init */
(function init() {
  setLimit(0);
  resetUploadState();
  showUploadView();
  poll();
})();
