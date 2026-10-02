"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const SEVERITY_LABEL = { error: "Problem", warning: "Likely issue", check: "Please check", manual: "Your note" };
const STATUS_LABEL = { open: "To review", confirmed: "Confirmed", fixed: "Fixed", dismissed: "Dismissed" };
const state = {
  project: null, issues: [], review: null, fps: 30, duration: 0,
  pollTimer: null, saveTimer: null,
  issueAiSuggestions: {},
  duplicateGroups: [],
  duplicateReviewHidden: false,
  assistantMessages: [],
  assistantContextIssueId: null,
  assistantThinking: false,
  assistantAttachments: [],
  assistantPreparingImages: false,
    reviewHistory: [],
  reviewHistoryIndex: -1,
  reviewHistoryGroup: null,
  pendingReviewSave: null,
};
const player = $("#player");

// ---------------------------------------------------------------- helpers
async function api(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function setAssistantIssueContext(issue) {
  state.assistantContextIssueId = issue?.id || null;
  const context = $("#assistantIssueContext");
  if (!issue) {
    context.hidden = true;
    $("#assistantIssueContextLabel").textContent = "";
    return;
  }
  $("#assistantIssueContextLabel").textContent =
    `Asking about ${issue.category} at ${timecode(issue.time)}: ${issue.title}`;
  context.hidden = false;
}

function timecode(sec) {
  const fpsWhole = Math.max(1, Math.round(state.fps));
  const frames = Math.round(sec * state.fps);
  const ff = frames % fpsWhole;
  let s = Math.floor(frames / fpsWhole);
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  return [h, m, s, ff].map((v) => String(v).padStart(2, "0")).join(":");
}

function realTime(sec) {
  const m = Math.floor(sec / 60);
  return `${String(m).padStart(2, "0")}:${(sec - m * 60).toFixed(2).padStart(5, "0")}`;
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 3000);
}

function confirmAction(message, { title = "Are you sure?", confirmLabel = "Confirm", danger = true } = {}) {
  const dialog = $("#appConfirmDialog");
  $("#appConfirmTitle").textContent = title;
  $("#appConfirmMessage").textContent = message;
  const accept = $("#appConfirmAccept");
  accept.textContent = confirmLabel;
  accept.classList.toggle("danger", danger);
  return new Promise((resolve) => {
    const onClose = () => {
      dialog.removeEventListener("close", onClose);
      resolve(dialog.returnValue === "confirm");
    };
    dialog.addEventListener("close", onClose);
    dialog.showModal();
  });
}

$("#appConfirmCancel").addEventListener("click", () => $("#appConfirmDialog").close("cancel"));
$("#appConfirmAccept").addEventListener("click", () => $("#appConfirmDialog").close("confirm"));

function showView(name) {
  for (const v of ["home", "progress", "review"]) $(`#${v}View`).hidden = v !== name;
  $("#reviewActions").hidden = name !== "review";
  if (name !== "review") player.pause();
}

function thumbUrl(sec) {
  const max = Math.max((state.project?.analysis?.thumb_count || 1) - 1, 0);
  return `/api/projects/${state.project.id}/thumb/${Math.min(Math.max(0, Math.floor(sec)), max)}`;
}

function seek(sec) {
  player.currentTime = Math.max(0, sec);
  player.pause();
}

// ------------------------------------------------------------------ home
let chosenFile = null;
let chosenScript = null;
let homeStatusTimer = null;
let storageItems = [];

async function loadAiReadiness() {
  const button = $("#aiReadinessRefresh");
  const textOutput = $("#aiTextReadiness");
  const visionOutput = $("#aiVisionReadiness");
  const textItem = $("#aiTextReadinessItem");
  const visionItem = $("#aiVisionReadinessItem");
  button.disabled = true;
  button.textContent = "Checking...";
  button.setAttribute("aria-busy", "true");
  textItem.dataset.state = "checking";
  visionItem.dataset.state = "checking";
  textOutput.textContent = "Checking model status...";
  visionOutput.textContent = "Checking Ollama...";
  try {
    const status = await api("/api/ai/status");
    const textDescriptions = {
      loaded: ["ready", "Loaded and ready"],
      cached: ["ready", "Downloaded and ready"],
      download_on_first_use: ["setup", "Downloads when first used"],
      unavailable: ["unavailable", `Unavailable${status.text.error ? `: ${status.text.error}` : ""}`],
    };
    const [textState, textDescription] = textDescriptions[status.text.state]
      || ["unknown", "Status unknown"];
    textItem.dataset.state = textState;
    textOutput.textContent = textDescription;

    if (status.vision.state === "ready") {
      visionItem.dataset.state = "ready";
      visionOutput.textContent = `Ready · ${status.vision.model}`;
    } else if (status.vision.state === "model_required") {
      visionItem.dataset.state = "setup";
      visionOutput.textContent = "Ollama is running; install a supported vision model.";
    } else {
      visionItem.dataset.state = "unavailable";
      visionOutput.textContent = `Unavailable: ${status.vision.error || "Ollama is not responding."}`;
    }
  } catch (error) {
    textItem.dataset.state = "unavailable";
    visionItem.dataset.state = "unavailable";
    textOutput.textContent = `Could not check status: ${error.message}`;
    visionOutput.textContent = `Could not check status: ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "Check";
    button.removeAttribute("aria-busy");
  }
}

$("#aiReadinessRefresh").addEventListener("click", loadAiReadiness);

async function loadHome() {
  await flushSave();
  clearInterval(state.pollTimer);
  clearTimeout(homeStatusTimer);
  state.project = null;
  history.replaceState(null, "", "/");
  $("#projectTitle").textContent = "";
  showView("home");
  const list = await api("/api/projects");
  loadAiReadiness();
  $("#projectList").innerHTML = list.length ? list.map((p) => {
    const st = p.status.state || "unknown";
    const reviewStatus = p.review_status === "done" ? "done" : "ongoing";
    const analysisActive = ["ready", "queued", "running", "pausing", "paused", "resuming"].includes(st);
    const analysisClass = st === "done" ? "completed" : st === "paused" ? "paused"
      : analysisActive ? "ongoing" : "failed";
    const label = st === "done" ? "Completed" : st === "error" ? "Failed"
      : st === "paused" ? "Paused" : st === "pausing" ? "Pausing..."
        : st === "resuming" ? "Resuming..."
          : analysisActive ? `Ongoing${st === "running" ? ` ${Math.round(p.status.percent)}%` : ""}` : st;
    const analysisAction = st === "running" ? `<button data-act="pause">Pause</button>`
      : st === "paused" ? `<button data-act="resume" class="primary">Resume</button>`
        : st === "pausing" ? `<button type="button" disabled>Pausing...</button>`
          : st === "resuming" ? `<button type="button" disabled>Resuming...</button>` : "";
    return `<div class="project" data-id="${esc(p.id)}">
      <span class="name">${esc(p.name)}</span>
      <span class="muted">${esc(p.created)}</span>
      <span class="pill analysis-${analysisClass}">${esc(label)}</span>
      <label class="project-review-status">
        <span class="visually-hidden">Review status</span>
        <select data-review-status data-saved-status="${reviewStatus}" class="status-${reviewStatus}" aria-label="Review status">
          <option value="ongoing" ${reviewStatus === "ongoing" ? "selected" : ""}>Ongoing</option>
          <option value="done" ${reviewStatus === "done" ? "selected" : ""}>Done</option>
        </select>
      </label>
      ${analysisAction}
      <button data-act="open" class="primary">Open</button>
      <button data-act="delete">Delete</button>
    </div>`;
  }).join("") : `<p class="muted">No videos yet. Add one above.</p>`;
  await loadStorage();
  if (list.some((project) => ["pausing", "resuming"].includes(project.status.state))) {
    homeStatusTimer = setTimeout(() => {
      if (!$("#homeView").hidden) loadHome();
    }, 1000);
  }
}

function selectedStorageIds() {
  return $$("[data-storage-id]:checked").map((input) => input.dataset.storageId);
}

function renderStorage(data) {
  storageItems = data.items || [];
  const total = storageItems.reduce((sum, item) => sum + Number(item.bytes || 0), 0);
  $("#storageSummary").textContent = storageItems.length
    ? `${storageItems.length} cleanup candidate${storageItems.length === 1 ? "" : "s"} · ${formatFileSize(total)} can be reclaimed`
    : "No removable orphan videos, dumps, or caches were found.";
  $("#storageList").innerHTML = storageItems.length ? storageItems.map((item) => `
    <label class="storage-item">
      <input type="checkbox" data-storage-id="${esc(item.id)}">
      <span class="storage-item-copy">
        <strong>${esc(item.label)}</strong>
        <span class="muted">${esc(item.reason)}</span>
        <code>${esc(item.path)}</code>
      </span>
      <span class="storage-size">${formatFileSize(item.bytes)}</span>
    </label>`).join("") : `<p class="muted storage-empty">Nothing needs cleaning.</p>`;
  $("#storageSelectAll").checked = false;
  updateStorageActions();
}

async function loadStorage() {
  try {
    renderStorage(await api("/api/storage"));
  } catch (error) {
    storageItems = [];
    $("#storageSummary").textContent = `Storage scan failed: ${error.message}`;
    $("#storageList").innerHTML = "";
    updateStorageActions();
  }
}

function updateStorageActions() {
  const selected = selectedStorageIds();
  $("#storageCleanBtn").disabled = selected.length === 0;
  $("#storageSelectAll").checked = storageItems.length > 0 && selected.length === storageItems.length;
}

$("#storageScanBtn").addEventListener("click", async () => {
  $("#storageScanBtn").disabled = true;
  $("#storageSummary").textContent = "Scanning local storage...";
  try { await loadStorage(); } finally { $("#storageScanBtn").disabled = false; }
});

$("#storageList").addEventListener("change", updateStorageActions);
$("#storageSelectAll").addEventListener("change", (event) => {
  $$('[data-storage-id]').forEach((input) => { input.checked = event.target.checked; });
  updateStorageActions();
});

$("#storageCleanBtn").addEventListener("click", async () => {
  const ids = selectedStorageIds();
  const selected = storageItems.filter((item) => ids.includes(item.id));
  const total = selected.reduce((sum, item) => sum + Number(item.bytes || 0), 0);
  if (!await confirmAction(
    `Permanently delete ${selected.length} selected item${selected.length === 1 ? "" : "s"} (${formatFileSize(total)})? This cannot be undone.`,
    { title: "Delete selected storage?", confirmLabel: "Delete permanently" },
  )) return;
  const button = $("#storageCleanBtn");
  button.disabled = true;
  try {
    const result = await api("/api/storage/cleanup", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: ids }),
    });
    if (result.failed?.length) toast(`${result.removed.length} moved; ${result.failed.length} could not be moved.`);
    else toast(`${result.removed.length} item${result.removed.length === 1 ? "" : "s"} permanently deleted.`);
    await loadStorage();
  } catch (error) {
    toast(error.message);
    updateStorageActions();
  }
});

$("#projectList").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const id = btn.closest(".project").dataset.id;
  if (["pause", "resume"].includes(btn.dataset.act)) {
    const action = btn.dataset.act;
    btn.disabled = true;
    btn.textContent = action === "pause" ? "Pausing..." : "Resuming...";
    try {
      await api(`/api/projects/${id}/${action}`, { method: "POST" });
      await loadHome();
    } catch (error) {
      toast(error.message);
      await loadHome();
    }
    return;
  }
  if (btn.dataset.act === "open") openProject(id);
  if (btn.dataset.act === "delete" && await confirmAction(
    "Delete this video and its review notes from the app?",
    { title: "Delete this video?", confirmLabel: "Delete video" },
  )) {
    try { await api(`/api/projects/${id}`, { method: "DELETE" }); } catch (err) { toast(err.message); }
    loadHome();
  }
});

$("#projectList").addEventListener("change", async (event) => {
  const select = event.target.closest("[data-review-status]");
  if (!select) return;
  const projectId = select.closest(".project").dataset.id;
  const previousStatus = select.dataset.savedStatus;
  select.disabled = true;
  try {
    const result = await api(`/api/projects/${projectId}/review-status`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review_status: select.value }),
    });
    select.dataset.savedStatus = result.review_status;
    select.className = `status-${result.review_status}`;
  } catch (error) {
    select.value = previousStatus;
    toast(`Review status could not be saved: ${error.message}`);
  } finally {
    select.disabled = false;
  }
});

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function chooseFile(file) {
  chosenFile = file;
  $("#fileName").textContent = file ? `${file.name} (${formatFileSize(file.size)})` : "";
  $("#startBtn").disabled = !file;
}

$("#fileInput").addEventListener("change", (e) => chooseFile(e.target.files[0]));
function chooseScript(file) {
  chosenScript = file || null;
  $("#scriptFileName").textContent = chosenScript
    ? `${chosenScript.name} (${formatFileSize(chosenScript.size)})`
    : "No script selected";
}

$("#scriptChoose").addEventListener("click", () => $("#scriptFileInput").click());
$("#scriptFileInput").addEventListener("change", (e) => chooseScript(e.target.files[0]));
let setupStorageWarningShown = false;
function warnSetupPreferenceStorage() {
  if (setupStorageWarningShown) return;
  setupStorageWarningShown = true;
  toast("Browser storage is unavailable; setup preferences will not be remembered.");
}
for (const [id, key, allowed] of [
  ["modelSelect", "video-qc-model", ["base", "small", "medium"]],
  ["langSelect", "video-qc-language", ["en", "auto"]],
  ["visualSampleCount", "video-qc-visual-sample-count", ["8", "12", "24"]],
]) {
  const select = $(`#${id}`);
  try {
    const saved = localStorage.getItem(key);
    if (allowed.includes(saved)) select.value = saved;
  } catch {
    warnSetupPreferenceStorage();
  }
  select.addEventListener("change", () => {
    try {
      localStorage.setItem(key, select.value);
    } catch {
      warnSetupPreferenceStorage();
    }
  });
}
const drop = $("#drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  if (e.dataTransfer.files[0]) chooseFile(e.dataTransfer.files[0]);
});

$("#startBtn").addEventListener("click", () => {
  if (!chosenFile) return;
  $("#startBtn").disabled = true;
  const box = $("#uploadProgress");
  box.hidden = false;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/projects");
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.setRequestHeader("X-Filename", encodeURIComponent(chosenFile.name));
  xhr.setRequestHeader("X-Model", $("#modelSelect").value);
  xhr.setRequestHeader("X-Language", $("#langSelect").value);
  xhr.setRequestHeader("X-Visual-Sample-Count", $("#visualSampleCount").value);
  xhr.upload.onprogress = (e) => {
    const pct = e.lengthComputable ? (e.loaded / e.total) * 100 : 0;
    box.querySelector(".bar > div").style.width = pct + "%";
    box.querySelector("span").textContent = `Loading video ${pct.toFixed(0)}%`;
  };
  xhr.onload = async () => {
    if (xhr.status !== 200) {
      box.hidden = true;
      $("#startBtn").disabled = false;
      let message = "Upload failed. Is the app window still open?";
      try { message = JSON.parse(xhr.responseText).error || message; } catch {}
      toast(message);
      return;
    }
    let projectId;
    try {
      projectId = JSON.parse(xhr.responseText).id;
      if (!projectId) throw new Error("The server did not return a project id.");
    } catch (error) {
      box.hidden = true;
      $("#startBtn").disabled = false;
      toast(`Upload failed: ${error.message}`);
      return;
    }
    for (const [file, endpoint, label] of [
      [chosenScript, "script", "Adding script..."],
    ]) {
      if (!file) continue;
      box.hidden = false;
      box.querySelector(".bar > div").style.width = "100%";
      box.querySelector("span").textContent = label;
      try {
        await api(`/api/projects/${projectId}/${endpoint}`, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) },
          body: file,
        });
      } catch (error) {
        box.hidden = true;
        chooseFile(null);
        chooseScript(null);
        $("#scriptFileInput").value = "";
        await loadHome();
        toast(`Video uploaded, but ${label.toLowerCase()} could not be saved. Analysis was not started: ${error.message}`);
        return;
      }
    }
    box.hidden = false;
    box.querySelector(".bar > div").style.width = "100%";
    box.querySelector("span").textContent = "Saving client instructions...";
    try {
      await api(`/api/projects/${projectId}/ai-instructions`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instructions: $("#preAnalysisInstructions").value }),
      });
    } catch (error) {
      box.hidden = true;
      chooseFile(null);
      chooseScript(null);
      $("#scriptFileInput").value = "";
      await loadHome();
      toast(`Video uploaded, but the client instructions could not be saved. Analysis was not started: ${error.message}`);
      return;
    }
    box.hidden = true;
    chooseFile(null);
    chooseScript(null);
    $("#scriptFileInput").value = "";
    $("#preAnalysisInstructions").value = "";
    openProject(projectId);
  };
  xhr.onerror = () => { box.hidden = true; $("#startBtn").disabled = false; toast("Upload failed."); };
  xhr.send(chosenFile);
});

// -------------------------------------------------------------- progress
let progressProjectId = "";

async function openProject(id) {
  await flushSave();
  clearInterval(state.pollTimer);
  progressProjectId = id;
  let data = await api(`/api/projects/${id}`);
  progressModel = data.meta?.model || "base";
  if (["ready", "analyzing"].includes(data.status.state)) {
    await api(`/api/projects/${id}/start`, { method: "POST" });
    data = await api(`/api/projects/${id}`);
  }
  history.replaceState(null, "", `/?p=${encodeURIComponent(id)}`);
  $("#projectTitle").textContent = data.meta.name;
  if (data.status.state === "done" && data.analysis) return startReview(data);
  showProgress(id, data.status);
  state.pollTimer = setInterval(async () => {
    const st = await api(`/api/projects/${id}/status`).catch(() => null);
    if (!st) return;
    if (st.state === "done") { clearInterval(state.pollTimer); openProject(id); }
    else showProgress(id, st);
  }, 1500);
}

let progressSignature = "";
let progressChangedAt = 0;
let progressModel = "base";
let progressRun = { projectId: "", state: "", startedAt: 0, samples: [] };

function formatProgressTime(seconds) {
  const totalMinutes = Math.floor(Math.max(0, seconds) / 60);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (totalMinutes) return `${totalMinutes}m`;
  return `${Math.max(1, Math.round(seconds))}s`;
}

function updateProgressCommentary(projectId, status, phase) {
  const now = Date.now();
  const percent = Number(status.percent) || 0;
  const previousSample = progressRun.samples.at(-1);
  const newRun = progressRun.projectId !== projectId
    || (status.state === "running" && progressRun.state !== "running")
    || (status.state === "running" && previousSample && percent < previousSample.percent);
  if (newRun) {
    progressRun = { projectId, state: status.state, startedAt: 0, samples: [] };
  }
  if (status.state === "running" && !progressRun.startedAt) progressRun.startedAt = now;

  if (status.state === "running") {
    const lastSample = progressRun.samples.at(-1);
    if (!lastSample || percent - lastSample.percent >= 0.05) {
      progressRun.samples.push({ time: now, percent });
    }
    progressRun.samples = progressRun.samples.filter((sample) => now - sample.time <= 180000);
  }
  progressRun.state = status.state;

  const elapsed = progressRun.startedAt ? (now - progressRun.startedAt) / 1000 : 0;
  const samples = progressRun.samples;
  const firstSample = samples[0];
  const lastSample = samples.at(-1);
  let remaining = null;
  if (firstSample && lastSample && lastSample.time - firstSample.time >= 30000
      && lastSample.percent > firstSample.percent) {
    const rate = (lastSample.percent - firstSample.percent) / ((lastSample.time - firstSample.time) / 1000);
    if (rate > 0) remaining = (100 - percent) / rate;
  }

  const device = status.message.match(/Transcribing narration on (GPU|CPU)/i)?.[1].toUpperCase();
  const modelNote = phase === "speech" ? `Using the ${progressModel} speech model. ` : "";
  const deviceNote = device
    ? `Speech transcription is running on ${device}; the estimate reflects this PC's observed speed. `
    : "Estimate is based on this PC's recent progress and may shift between stages. ";
  if (status.state === "queued") {
    $("#progressCommentary").textContent = "Analysis is queued; timing starts when processing begins.";
  } else if (status.state === "error") {
    $("#progressCommentary").textContent = "Analysis stopped before a reliable time estimate was available.";
  } else if (status.state === "pausing") {
    $("#progressCommentary").textContent = "Pause requested. The current operation is finishing before analysis pauses.";
  } else if (status.state === "paused") {
    $("#progressCommentary").textContent = `${formatProgressTime(elapsed)} elapsed. Analysis is paused at a safe checkpoint; press Resume when you are ready.`;
  } else if (status.state === "resuming") {
    $("#progressCommentary").textContent = "Resuming from the last safe checkpoint...";
  } else if (remaining !== null && Number.isFinite(remaining)) {
    $("#progressCommentary").textContent = `About ${formatProgressTime(remaining)} remaining at the recent pace (${formatProgressTime(elapsed)} elapsed). ${modelNote}${deviceNote}`;
  } else {
    $("#progressCommentary").textContent = `${formatProgressTime(elapsed)} elapsed. Measuring this run's speed before estimating the remaining time. ${modelNote}${deviceNote}`;
  }
}

function showProgress(id, st) {
  showView("progress");
  progressProjectId = id;
  const signature = `${id}|${st.state}|${st.phase}|${st.percent}|${st.phase_progress}|${st.message}`;
  if (signature !== progressSignature) {
    progressSignature = signature;
    progressChangedAt = Date.now();
  }
  const indeterminate = st.state === "running" && Date.now() - progressChangedAt >= 15000;
  $("#progressBar").parentElement.classList.toggle("indeterminate", indeterminate);
  $("#progressWaitHint").hidden = !indeterminate;
  $("#progressBar").style.width = (st.percent || 0) + "%";
  $("#progressPercent").textContent = `${Math.round(st.percent || 0)}%`;
  $("#progressMsg").textContent = st.message || "Waiting for the next task...";
  const phaseNames = {
    pre_review: "Reviewing script and client instructions",
    audio: "Checking audio",
    motion: "Scanning video structure",
    speech: "Transcribing narration",
    frames: "Scanning frames and on-screen text",
    checks: "Comparing narration and visuals",
    text_ai: "Running local AI text cross-check",
    visual_review: "Running local AI visual pre-review",
    done: "Analysis complete",
  };
  const stages = Array.from(document.querySelectorAll("#analysisStages [data-phase]"));
  const overallPercent = Number(st.percent) || 0;
  let activePhase = st.phase;
  if (!phaseNames[activePhase]) {
    activePhase = overallPercent < 4.7 ? "audio" : overallPercent < 23.5 ? "motion"
      : overallPercent < 51.7 ? "speech" : overallPercent < 89.3 ? "frames"
        : overallPercent < 94 ? "checks" : overallPercent < 99 ? "text_ai" : "visual_review";
  }
  const phaseRanges = {
    pre_review: [0, 1],
    audio: [0, 4.7], motion: [4.7, 23.5], speech: [23.5, 51.7],
    frames: [51.7, 89.3], checks: [89.3, 94], text_ai: [95, 99], visual_review: [99, 100],
  };
  const phaseRange = phaseRanges[activePhase] || [0, 100];
  const hasPhaseProgress = st.phase_progress !== undefined && st.phase_progress !== null
    && Number.isFinite(Number(st.phase_progress));
  const phaseProgress = Math.round(Math.max(0, Math.min(100, hasPhaseProgress
    ? Number(st.phase_progress)
    : (overallPercent - phaseRange[0]) / (phaseRange[1] - phaseRange[0]) * 100)));
  const activeIndex = stages.findIndex((stage) => stage.dataset.phase === activePhase);
  $("#progressPhase").textContent = phaseNames[activePhase] || "Preparing analysis";
  updateProgressCommentary(id, st, activePhase);
  const pauseButton = $("#progressPauseBtn");
  const canPause = ["running", "pausing", "paused", "resuming"].includes(st.state);
  pauseButton.hidden = !canPause;
  pauseButton.disabled = ["pausing", "resuming"].includes(st.state);
  pauseButton.dataset.action = st.state === "paused" ? "resume" : "pause";
  pauseButton.textContent = st.state === "paused" ? "Resume"
    : st.state === "pausing" ? "Pausing..." : st.state === "resuming" ? "Resuming..." : "Pause";
  stages.forEach((stage, index) => {
    const complete = st.state === "done" || index < activeIndex;
    const active = st.state !== "done" && index === activeIndex;
    stage.classList.toggle("complete", complete);
    stage.classList.toggle("active", active);
    stage.classList.toggle("waiting", !complete && !active);
    stage.querySelector(".stage-mark").textContent = complete ? "Done" : active ? "Now" : "Waiting";
    stage.querySelector(".stage-state").textContent = complete ? "Complete" : active
      ? `In progress ${phaseProgress}%` : "Queued";
  });
  const failed = st.state === "error";
  $("#progressTitle").textContent = failed ? "Analysis stopped" : st.state === "done" ? "Analysis complete"
    : st.state === "paused" ? "Analysis paused" : st.state === "pausing" ? "Pausing analysis..."
      : st.state === "resuming" ? "Resuming analysis..." : "Analysing your video...";
  $("#progressError").hidden = !failed;
  $("#progressErrorText").textContent = st.error || "";
  $("#rerunBtn").onclick = async () => { await api(`/api/projects/${id}/rerun`, { method: "POST" }); openProject(id); };
}

$("#progressPauseBtn").addEventListener("click", async () => {
  const button = $("#progressPauseBtn");
  const action = button.dataset.action;
  if (!progressProjectId || !["pause", "resume"].includes(action)) return;
  button.disabled = true;
  try {
    await api(`/api/projects/${progressProjectId}/${action}`, { method: "POST" });
    const status = await api(`/api/projects/${progressProjectId}/status`);
    showProgress(progressProjectId, status);
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
});

// ---------------------------------------------------------------- review
function startReview(data) {
  state.project = data;
  updateAssistantScriptStatus(data.meta || {});
  state.issueAiSuggestions = {};
  state.duplicateGroups = [];
  state.assistantContextIssueId = null;
  setAssistantIssueContext(null);
  try {
    const messages = JSON.parse(localStorage.getItem(`video-qc-assistant:${data.id}`) || "[]");
    const localMessages = Array.isArray(messages) ? messages.slice(-40).map((message) => ({
      ...message,
      images: Array.isArray(message.images) ? message.images.filter(isAssistantPreview) : [],
    })) : [];
    if (data.assistant_history_saved && Array.isArray(data.assistant_history)) {
      state.assistantMessages = data.assistant_history.slice(-40).map((message) => {
        const localMessage = localMessages.find((item) =>
          item.role === message.role && item.content === message.content
        );
        return { ...message, images: localMessage?.images || [] };
      });
    } else {
      state.assistantMessages = localMessages;
    }
  } catch {
    state.assistantMessages = Array.isArray(data.assistant_history) ? data.assistant_history.slice(-40) : [];
  }
  state.assistantAttachments = [];
  renderAssistantAttachments();
  state.fps = data.analysis.info.fps || 30;
  state.duration = data.analysis.info.duration || 0;
  state.review = Object.assign({ reviewer: "", summary: "", status: {}, manual: [], checklist: {}, overrides: {} }, data.review);
  const oldPacingItem = "Scene changes every 5-6 seconds";
  const pacingItem = "Scene pacing feels purposeful and fits the content";
  if (state.review.checklist[oldPacingItem] && !state.review.checklist[pacingItem]) {
    state.review.checklist[pacingItem] = state.review.checklist[oldPacingItem];
  }
  delete state.review.checklist[oldPacingItem];
  state.issues = data.issues;
  state.reviewHistory = [{ review: structuredClone(state.review), issues: structuredClone(state.issues) }];
  state.reviewHistoryIndex = 0;
  state.reviewHistoryGroup = null;
  updateHistoryButtons();
  try {
    const savedDuplicates = JSON.parse(localStorage.getItem(`video-qc-duplicates:${data.id}`) || "null");
    if (savedDuplicates && Array.isArray(savedDuplicates.groups)) {
      const openIds = new Set(state.issues.filter((issue) => issue.status === "open").map((issue) => issue.id));
      state.duplicateGroups = savedDuplicates.groups.filter((group) =>
        openIds.has(group.keep) && Array.isArray(group.dismiss)
        && group.dismiss.some((id) => openIds.has(id))
      ).map((group) => Object.assign({}, group, {
        dismiss: group.dismiss.filter((id) => openIds.has(id)),
      }));
      state.duplicateReviewHidden = Boolean(savedDuplicates.hidden);
    }
  } catch {
    state.duplicateGroups = [];
    state.duplicateReviewHidden = false;
  }
  $("#filterStatus").value = "active";
  showView("review");
  $("#saveState").textContent = "";
  if (!player.src.endsWith(`/api/projects/${data.id}/video`)) player.src = `/api/projects/${data.id}/video`;

  const cats = data.categories.map((c) => `<option>${esc(c)}</option>`).join("");
  $("#addCategory").innerHTML = cats;
  $("#addCategory").value = "Other";
  $("#filterCategory").innerHTML = `<option value="">All categories</option>` + cats;
  $("#issueCategories").innerHTML = data.categories.map((category) => `<option value="${esc(category)}"></option>`).join("");

  $("#warnings").innerHTML = (data.analysis.warnings || []).map((w) => `<div class="warn">${esc(w)}</div>`).join("");
  const preReview = data.pre_review;
  const preReviewElement = $("#preReviewResult");
  if (preReview?.state === "done") {
    const checks = (preReview.result?.checks || []).map((item) => `<li>${esc(item)}</li>`).join("");
    const conflicts = (preReview.result?.conflicts || []).map((item) =>
      `<li><strong>${esc(item.script_quote || "Script discrepancy")}:</strong> ${esc(item.reference_quote || "")} ${esc(item.explanation || "")}</li>`
    ).join("");
    preReviewElement.innerHTML = `<strong>Script pre-review complete.</strong>${preReview.result?.summary ? ` ${esc(preReview.result.summary)}` : ""}${checks ? `<p>Carry these checks into the review:</p><ul>${checks}</ul>` : ""}${conflicts ? `<p>Possible script/instruction conflicts to verify:</p><ul>${conflicts}</ul>` : ""}`;
    preReviewElement.hidden = false;
  } else if (preReview?.state === "error") {
    preReviewElement.textContent = `Script pre-review was unavailable: ${preReview.message}`;
    preReviewElement.hidden = false;
  } else {
    preReviewElement.hidden = true;
    preReviewElement.textContent = "";
  }
  const visualReview = data.visual_review || {};
  const visualReviewElement = $("#visualReviewResult");
  const visualResults = Array.isArray(visualReview.results) ? visualReview.results : [];
  const visualConcerns = visualResults.filter((item) => item.assessment === "possible concern");
  const highConfidence = visualConcerns.filter((item) => item.confidence === "high").length;
  const needsManualCheck = visualConcerns.length - highConfidence;
  if (visualReview.state === "done") {
    const totalShots = Number(visualReview.total_shots) || data.analysis.shots?.length || 0;
    const reviewed = Number(visualReview.done) || visualResults.length;
    const sampled = Number(visualReview.total) || reviewed;
    const findings = visualConcerns.length
      ? `${highConfidence} likely concern${highConfidence === 1 ? "" : "s"} and ${needsManualCheck} uncertain finding${needsManualCheck === 1 ? "" : "s"} need review.`
      : "No possible visual mismatches were identified in the sampled shots.";
    visualReviewElement.textContent = `Visual pre-review · ${visualReview.model || "local vision model"} · Checked ${reviewed} of ${totalShots} detected shots (${sampled} sampled). ${findings} Sampling prioritizes script/narration matches and coverage; other shots were not individually AI-checked.`;
    visualReviewElement.classList.remove("warning");
    visualReviewElement.hidden = false;
  } else if (visualReview.state === "skipped" || visualReview.state === "error") {
    const partial = visualConcerns.length
      ? ` ${visualConcerns.length} partial finding${visualConcerns.length === 1 ? "" : "s"} may still appear below.`
      : "";
    const model = visualReview.model ? ` (${visualReview.model})` : "";
    const coverage = visualReview.state === "error"
      ? ` Checked ${Number(visualReview.done) || visualResults.length} of ${Number(visualReview.total_shots) || data.analysis.shots?.length || 0} detected shots before stopping.`
      : "";
    visualReviewElement.textContent = `Visual pre-review ${visualReview.state === "skipped" ? "not run" : "stopped"}${model}: ${visualReview.message || visualReview.error || "No details were provided."}${coverage}${partial}`;
    visualReviewElement.classList.add("warning");
    visualReviewElement.hidden = false;
  } else {
    visualReviewElement.hidden = true;
    visualReviewElement.textContent = "";
  }
  $("#reviewer").value = state.review.reviewer || "";
  $("#summary").value = state.review.summary || "";

  renderIssues();
  renderDuplicateGroups(state.duplicateGroups);
  renderHighlights();
  renderTranscript();
  renderAssistantMessages();
  renderTimeline();
}

function updateHistoryButtons() {
  $("#undoBtn").disabled = state.reviewHistoryIndex <= 0;
  $("#redoBtn").disabled = state.reviewHistoryIndex < 0 || state.reviewHistoryIndex >= state.reviewHistory.length - 1;
}

function recordReviewHistory(group = null) {
  const snapshot = { review: structuredClone(state.review), issues: structuredClone(state.issues) };
  if (state.reviewHistoryIndex < state.reviewHistory.length - 1) {
    state.reviewHistory = state.reviewHistory.slice(0, state.reviewHistoryIndex + 1);
  }
  if (group && group === state.reviewHistoryGroup && state.reviewHistoryIndex > 0) {
    state.reviewHistory[state.reviewHistoryIndex] = snapshot;
  } else {
    state.reviewHistory.push(snapshot);
    state.reviewHistoryIndex = state.reviewHistory.length - 1;
  }
  state.reviewHistoryGroup = group;
  updateHistoryButtons();
}

function restoreReviewHistory(index) {
  const snapshot = state.reviewHistory[index];
  if (!snapshot) return;
  state.reviewHistoryIndex = index;
  state.reviewHistoryGroup = null;
  state.review = structuredClone(snapshot.review);
  state.issues = structuredClone(snapshot.issues);
  $("#reviewer").value = state.review.reviewer || "";
  $("#summary").value = state.review.summary || "";
  saveReview(null, false);
  renderIssues();
  renderTimeline();
  updateHistoryButtons();
}

function saveReview(historyGroup = null, recordHistory = true) {
  if (!state.project || !state.review) return;
  if (recordHistory) recordReviewHistory(historyGroup);
  state.pendingReviewSave = {
    projectId: state.project.id,
    review: JSON.stringify(state.review),
  };
  $("#saveState").textContent = "Saving...";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => flushSave(), 500);
}

async function flushSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = null;
  const pending = state.pendingReviewSave;
  if (!pending) return;
  state.pendingReviewSave = null;
  try {
    await api(`/api/projects/${pending.projectId}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: pending.review,
    });
    if (state.project?.id === pending.projectId) $("#saveState").textContent = "All changes saved";
  } catch (error) {
    if (!state.pendingReviewSave) state.pendingReviewSave = pending;
    if (state.project?.id === pending.projectId) {
      $("#saveState").textContent = `Save failed. Try again: ${error.message}`;
    }
  }
}

$("#undoBtn").addEventListener("click", () => {
  if (state.reviewHistoryIndex > 0) restoreReviewHistory(state.reviewHistoryIndex - 1);
});

$("#redoBtn").addEventListener("click", () => {
  if (state.reviewHistoryIndex < state.reviewHistory.length - 1) restoreReviewHistory(state.reviewHistoryIndex + 1);
});

function sevOf(issue) { return issue.source === "manual" ? "manual" : issue.severity; }

// ---- timeline
function renderTimeline() {
  const tl = $("#timeline");
  tl.querySelectorAll(".cut, .mark").forEach((n) => n.remove());
  const pct = (t) => `${(t / state.duration) * 100}%`;
  let html = "";
  for (const s of state.project.analysis.shots) {
    if (s.start > 0) html += `<div class="cut" style="left:${pct(s.start)}"></div>`;
  }
  for (const i of state.issues) {
    const width = i.end ? pct(Math.max(i.end - i.time, 0)) : "3px";
    html += `<div class="mark sev-${sevOf(i)} ${i.status}" data-id="${esc(i.id)}"
      style="left:${pct(i.time)};width:${width}" title="${esc(timecode(i.time) + "  " + i.title)}"></div>`;
  }
  tl.insertAdjacentHTML("beforeend", html);
}

$("#timeline").addEventListener("click", (e) => {
  const mark = e.target.closest(".mark");
  if (mark) {
    const issue = state.issues.find((i) => i.id === mark.dataset.id);
    if (issue) { seek(issue.time); focusIssue(issue.id); return; }
  }
  const rect = e.currentTarget.getBoundingClientRect();
  seek(((e.clientX - rect.left) / rect.width) * state.duration);
});

// ---- issues
function filteredIssues() {
  const cat = $("#filterCategory").value, st = $("#filterStatus").value, sev = $("#filterSeverity").value;
  return state.issues.filter((i) =>
    (!cat || i.category === cat) &&
    (!sev || sevOf(i) === sev) &&
    (!st || (st === "active" ? ["open", "confirmed"].includes(i.status) : i.status === st)));
}

function renderIssues() {
  const list = filteredIssues();
  const open = state.issues.filter((i) => i.status === "open").length;
  $("#issueCount").textContent = open;
  $("#issueList").innerHTML = list.length ? list.map(issueHtml).join("") :
    `<div class="empty">Nothing here. Change the filters above or add your own notes.</div>`;
}

function renderDuplicateGroups(groups) {
  state.duplicateGroups = groups;
  if (state.project) {
    try {
      const key = `video-qc-duplicates:${state.project.id}`;
      if (groups.length) {
        localStorage.setItem(key, JSON.stringify({ groups, hidden: state.duplicateReviewHidden }));
      } else {
        localStorage.removeItem(key);
      }
    } catch {
      // Keep the current session usable if browser storage is unavailable.
    }
  }
  const repeatCount = groups.reduce((total, group) => total + group.dismiss.length, 0);
  $("#dedupeDismissAll").hidden = repeatCount === 0;
  $("#dedupeDismissAll").textContent = `Dismiss all ${repeatCount} repeats`;
  $("#dedupeCloseReview").hidden = repeatCount === 0;
  $("#dedupeCloseReview").textContent = state.duplicateReviewHidden
    ? `Reopen review (${groups.length} groups)` : "Close for now";
  $("#dedupeResults").hidden = state.duplicateReviewHidden;
  $("#dedupeResults").innerHTML = groups.map((group, index) => {
    const keep = state.issues.find((issue) => issue.id === group.keep);
    const duplicates = group.dismiss.map((id) => state.issues.find((issue) => issue.id === id)).filter(Boolean);
    if (!keep || !duplicates.length) return "";
    return `<article class="duplicate-group">
      <strong>Keep ${timecode(keep.time)}: ${esc(keep.title)}</strong>
      <p>Repeated at ${duplicates.map((issue) => timecode(issue.time)).join(", ")}</p>
      <p class="muted">${esc(group.reason)}</p>
      <div class="actions">
        <button data-dismiss-repeats="${index}">Dismiss repeats</button>
        <button data-keep-repeats="${index}">Keep all</button>
      </div>
    </article>`;
  }).join("");
}

function dismissDuplicateGroups(groups) {
  let dismissedCount = 0;
  let groupCount = 0;
  state.review.overrides ||= {};
  for (const group of groups) {
    const keep = state.issues.find((issue) => issue.id === group.keep && issue.status === "open");
    const duplicates = group.dismiss.map((id) => state.issues.find((issue) => issue.id === id && issue.status === "open")).filter(Boolean);
    if (!keep || !duplicates.length) continue;

    const occurrenceTimes = [...new Set(duplicates.map((issue) => timecode(issue.time)))];
    const existing = state.review.overrides[keep.id] || {};
    const detail = existing.detail ?? keep.detail ?? "";
    const repeatedDetail = `${detail}${detail ? " " : ""}Repeated occurrences: ${occurrenceTimes.join(", ")}.`;
    state.review.overrides[keep.id] = Object.assign({}, existing, { detail: repeatedDetail });
    keep.detail = repeatedDetail;
    for (const issue of duplicates) {
      issue.status = "dismissed";
      state.review.status[issue.id] = { status: "dismissed", note: issue.note || "" };
    }
    dismissedCount += duplicates.length;
    groupCount += 1;
  }
  if (dismissedCount) {
    saveReview();
    renderIssues();
    renderTimeline();
  }
  return { dismissedCount, groupCount };
}

function issueHtml(i) {
  const range = i.end ? ` &rarr; ${timecode(i.end)}` : "";
  const occurrences = Array.isArray(i.occurrences) ? i.occurrences : [];
  const occurrenceLinks = occurrences.length > 1
    ? `<div class="issue-occurrences muted">Also flagged: ${occurrences.slice(1).map((item) =>
      `<button type="button" class="occurrence-time" data-seek="${Number(item.time) || 0}">${timecode(item.time)}</button>`
    ).join(" ")}</div>` : "";
  const btn = (st, label) => `<button data-st="${st}" class="${i.status === st ? "on" : ""}">${label}</button>`;
  const suggestion = i.suggestions;
  const aiSuggestion = state.issueAiSuggestions[i.id];
  const notePlaceholder = suggestion?.correction
    ? `Auto-suggestion (used in export if left blank): ${suggestion.correction}`
    : "Notes / fix for the editor...";
  const confidence = String(i.confidence || "low");
  const confidenceLabel = confidence === "high"
    ? "High confidence · likely issue"
    : `${confidence.charAt(0).toUpperCase()}${confidence.slice(1)} confidence · please check`;
  const visualConfidence = i.source === "visual_ai"
    ? `<span class="visual-confidence">AI visual suggestion · ${esc(confidenceLabel)}</span>`
    : "";
  return `<div class="issue sev-${sevOf(i)} st-${i.status}" data-id="${esc(i.id)}">
    <img src="${thumbUrl(i.time)}" loading="lazy" alt="" data-seek="${i.time}">
    <div>
      <div class="head">
        <button class="tc" data-seek="${i.time}">${timecode(i.time)}</button>
        <span class="cat">${esc(i.category)}${range ? `<span class="muted">${range}</span>` : ""}</span>
        <span class="status-tag">${SEVERITY_LABEL[sevOf(i)]} &middot; ${STATUS_LABEL[i.status]}</span>
        ${visualConfidence}
      </div>
      <div class="title">${esc(i.title)}</div>
      ${i.detail ? `<div class="detail">${esc(i.detail)}</div>` : ""}
      ${occurrenceLinks}
    </div>
    <details class="issue-editor full">
      <summary>Edit category or finding</summary>
      <div class="issue-edit-grid">
        ${suggestion?.category ? `<p class="edit-wide muted">Suggested category: <b>${esc(suggestion.category)}</b> <button type="button" data-suggest-category="${esc(suggestion.category)}">Use category</button></p>` : ""}
        <label>Category<input list="issueCategories" data-edit-field="category" value="${esc(i.category)}"></label>
        <label>Finding title<input data-edit-field="title" value="${esc(i.title)}"></label>
        <label class="edit-wide">Finding detail<textarea rows="2" data-edit-field="detail">${esc(i.detail || "")}</textarea></label>
        ${aiSuggestion?.category ? `<p class="edit-wide muted">AI suggests category: <b>${esc(aiSuggestion.category)}</b> ${aiSuggestion.reason ? `&middot; ${esc(aiSuggestion.reason)}` : ""} <button type="button" data-suggest-category="${esc(aiSuggestion.category)}">Use category</button></p>` : ""}
        <button type="button" data-ai-suggestion title="Uses the local text model; first use may download about 1.12 GB.">AI suggest Notes / fix</button>
        <button type="button" data-save-finding>Save finding edits</button>
      </div>
    </details>
    <textarea class="full" rows="1" placeholder="${esc(notePlaceholder)}">${esc(i.note)}</textarea>
    <div class="actions full">
      <button type="button" data-ask-assistant>Ask assistant about this</button>
      ${btn("confirmed", "Confirm problem")}${btn("dismissed", "Not a problem")}
      ${i.status !== "open" ? `<button data-st="open">Undo</button>` : ""}
      ${i.source === "manual" ? `<button data-del="1">Delete note</button>` : ""}
    </div>
  </div>`;
}

function setIssue(id, changes, historyGroup = null) {
  const issue = state.issues.find((i) => i.id === id);
  Object.assign(issue, changes);
  if (issue.source === "manual") {
    const m = state.review.manual.find((x) => x.id === id);
    if (m && changes.title !== undefined) m.title = changes.title;
  }
  state.review.status[id] = { status: issue.status, note: issue.note };
  saveReview(historyGroup);
}

function saveFindingOverride(card) {
  const issue = state.issues.find((item) => item.id === card.dataset.id);
  if (!issue) return;
  const fields = Object.fromEntries(Array.from(card.querySelectorAll("[data-edit-field]")).map((input) => [input.dataset.editField, input.value.trim()]));
  if (!state.project.categories.includes(fields.category)) {
    toast("Choose an issue type from the suggestions list.");
    return;
  }
  if (!fields.title) {
    toast("A finding title is required.");
    return;
  }
  state.review.overrides ||= {};
  state.review.overrides[issue.id] = fields;
  Object.assign(issue, fields);
  saveReview();
  renderIssues();
  renderTimeline();
}

$("#issueList").addEventListener("click", async (e) => {
  const seekEl = e.target.closest("[data-seek]");
  if (seekEl) return seek(parseFloat(seekEl.dataset.seek));
  const card = e.target.closest(".issue");
  const btn = e.target.closest("button");
  if (!card || !btn) return;
  const id = card.dataset.id;
  if (btn.dataset.askAssistant !== undefined) {
    const issue = state.issues.find((item) => item.id === id);
    if (!issue) return;
    setAssistantIssueContext(issue);
    switchTab("assistant");
    $("#assistantInput").focus();
  } else if (btn.dataset.aiSuggestion !== undefined) {
    const noteField = card.querySelector("textarea:not([data-edit-field])");
    if (noteField.value.trim() && !await confirmAction(
      "Replace the existing Notes / fix text with the AI suggestion?",
      { title: "Replace your note?", confirmLabel: "Replace note", danger: false },
    )) return;
    const oldLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Generating...";
    try {
      const result = await api(`/api/projects/${state.project.id}/issues/${encodeURIComponent(id)}/suggestion`, {
        method: "POST",
      });
      state.issueAiSuggestions[id] = result;
      setIssue(id, { note: result.correction });
      renderIssues();
      renderTimeline();
      const updatedCard = $(`.issue[data-id="${CSS.escape(id)}"]`);
      if (updatedCard) {
        updatedCard.querySelector(".issue-editor").open = true;
        updatedCard.querySelector("textarea:not([data-edit-field])").focus();
      }
    } catch (error) {
      toast(error.message);
      btn.disabled = false;
      btn.textContent = oldLabel;
    }
  } else if (btn.dataset.saveFinding !== undefined) {
    saveFindingOverride(card);
  } else if (btn.dataset.suggestCategory) {
    const category = btn.dataset.suggestCategory;
    if (!state.project.categories.includes(category)) return;
    state.review.overrides ||= {};
    state.review.overrides[id] = Object.assign({}, state.review.overrides[id], { category });
    const issue = state.issues.find((item) => item.id === id);
    issue.category = category;
    if (issue.suggestions) issue.suggestions.category = null;
    saveReview();
    renderIssues();
    renderTimeline();
  } else if (btn.dataset.st) {
    setIssue(id, { status: btn.dataset.st });
    if (btn.dataset.st === "dismissed") $("#filterStatus").value = "active";
    renderIssues();
    renderTimeline();
  } else if (btn.dataset.del && await confirmAction(
    "Delete this note?",
    { title: "Delete this note?", confirmLabel: "Delete note" },
  )) {
    state.review.manual = state.review.manual.filter((m) => m.id !== id);
    delete state.review.status[id];
    delete state.review.overrides?.[id];
    state.issues = state.issues.filter((i) => i.id !== id);
    saveReview();
    renderIssues();
    renderTimeline();
  }
});

$("#issueList").addEventListener("input", (e) => {
  if (e.target.tagName !== "TEXTAREA" || e.target.dataset.editField) return;
  const id = e.target.closest(".issue").dataset.id;
  setIssue(id, { note: e.target.value }, `note:${id}`);
});

for (const id of ["#filterCategory", "#filterStatus", "#filterSeverity"]) $(id).addEventListener("change", renderIssues);

function focusIssue(id) {
  switchTab("issues");
  let card = $(`.issue[data-id="${CSS.escape(id)}"]`);
  if (!card) {
    $("#filterCategory").value = ""; $("#filterStatus").value = ""; $("#filterSeverity").value = "";
    renderIssues();
    card = $(`.issue[data-id="${CSS.escape(id)}"]`);
  }
  card?.scrollIntoView({ block: "center", behavior: "smooth" });
}

function addManualIssue() {
  const text = $("#addText").value.trim();
  const category = $("#addCategory").value;
  const t = player.currentTime;
  const issue = {
    id: `m${Date.now()}`, category, time: Math.round(t * 1000) / 1000, end: null,
    severity: "warning", title: text || category, detail: "", source: "manual",
  };
  state.review.manual.push(issue);
  state.issues.push(Object.assign({}, issue, { status: "open", note: "" }));
  state.issues.sort((a, b) => a.time - b.time);
  state.review.status[issue.id] = { status: "open", note: "" };
  saveReview();
  $("#addText").value = "";
  renderIssues();
  renderTimeline();
  toast(`Note added at ${timecode(t)}`);
}

$("#addBtn").addEventListener("click", addManualIssue);
$("#addText").addEventListener("keydown", (e) => { if (e.key === "Enter") addManualIssue(); });

// ---- highlights
function renderHighlights() {
  const rows = state.project.analysis.highlights || [];
  $("#highlightList").innerHTML = rows.length ? `<table class="hl">
    <tr><th>Time</th><th>Type</th><th>Narrator says</th><th>On screen</th><th>Result</th></tr>
    ${rows.map((h) => `<tr data-seek="${h.time}">
      <td class="tc">${timecode(h.time)}</td><td>${esc(h.kind)}</td><td>${esc(h.said)}</td>
      <td>${h.shown ? esc(h.shown) : `<span class="muted">nothing found</span>`}</td>
      <td><span class="st ${esc(h.status.replace(" ", "-"))}">${esc(h.status)}</span></td></tr>`).join("")}
  </table>` : `<div class="empty">No years, percentages, measures or names were detected in the narration.</div>`;
}

$("#highlightList").addEventListener("click", (e) => {
  const row = e.target.closest("[data-seek]");
  if (row) seek(parseFloat(row.dataset.seek));
});

// ---- transcript
function renderTranscript() {
  const segs = state.project.analysis.transcript || [];
  $("#transcriptList").innerHTML = segs.length ? segs.map((s, n) =>
    `<p data-seek="${s.start}" data-n="${n}"><span class="tc">${timecode(s.start)}</span>${esc(s.text)}</p>`).join("") :
    `<div class="empty">No narration transcript.</div>`;
}

function renderAssistantMessages() {
  const log = $("#assistantMessages");
  if (!state.assistantMessages.length) {
    log.innerHTML = `<div class="assistant-message"><span class="message-role">Assistant</span><p>Ask about findings, transcript, script, or what to review next. You can also ask me to dismiss all findings in a category. Attach an image only when you want visual inspection.</p></div>`;
  } else {
    log.innerHTML = state.assistantMessages.map((message) =>
      `<div class="assistant-message ${message.role === "user" ? "user" : ""}"><span class="message-role">${message.role === "user" ? "You" : "Assistant"}</span><p>${esc(message.content)}</p>${message.images?.length ? `<div class="assistant-message-images">${message.images.filter(isAssistantPreview).map((image) => `<img src="${esc(image)}" alt="Attached image">`).join("")}</div>` : ""}</div>`
    ).join("");
  }
  if (state.assistantThinking) {
    log.insertAdjacentHTML("beforeend", `<div class="assistant-message assistant-thinking" role="status" aria-label="Assistant is writing">
      <span class="thinking-dots" aria-hidden="true"><i></i><i></i><i></i></span>
    </div>`);
  }
  log.scrollTop = log.scrollHeight;
}

function isAssistantPreview(value) {
  return typeof value === "string" && /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(value);
}

function renderAssistantAttachments() {
  $("#assistantAttachments").innerHTML = state.assistantAttachments.map((image, index) =>
    `<div class="assistant-attachment"><img src="${esc(image.preview)}" alt="${esc(image.name)}"><button type="button" data-remove-image="${index}" aria-label="Remove ${esc(image.name)}">×</button></div>`
  ).join("");
}

async function prepareAssistantImage(file) {
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
    throw new Error(`${file.name} is not a supported image. Choose a JPEG, PNG, or WebP.`);
  }
  if (file.size > 12 * 1024 * 1024) {
    throw new Error(`${file.name} is larger than 12 MB.`);
  }
  const bitmap = await createImageBitmap(file);
  try {
    const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Could not prepare the selected image.");
    context.fillStyle = "#fff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const data = canvas.toDataURL("image/jpeg", 0.82);
    if ((data.length * 3) / 4 > 5 * 1024 * 1024) {
      throw new Error(`${file.name} is too large after image preparation. Choose a smaller image.`);
    }
    const preview = document.createElement("canvas");
    const previewScale = Math.min(1, 240 / Math.max(canvas.width, canvas.height));
    preview.width = Math.max(1, Math.round(canvas.width * previewScale));
    preview.height = Math.max(1, Math.round(canvas.height * previewScale));
    const previewContext = preview.getContext("2d");
    if (!previewContext) throw new Error("Could not prepare the image preview.");
    previewContext.drawImage(canvas, 0, 0, preview.width, preview.height);
    return { data, preview: preview.toDataURL("image/jpeg", 0.5), name: file.name };
  } finally {
    bitmap.close();
  }
}

function updateAssistantScriptStatus(meta) {
  const scriptName = meta.script_name || "";
  $("#assistantScriptStatus").textContent = scriptName
    ? `${scriptName}${meta.script_char_count ? ` · ${Number(meta.script_char_count).toLocaleString()} characters` : ""}`
    : "No script attached to this project.";
  $("#reviewScriptRemove").hidden = !scriptName;
  $("#reviewScriptChoose").textContent = scriptName ? "Replace script" : "Attach PDF/DOCX";
}

$("#reviewScriptChoose").addEventListener("click", () => $("#reviewScriptInput").click());

$("#reviewScriptInput").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file || !state.project) return;
  if (file.size > 15 * 1024 * 1024) {
    toast("Script files must be 15 MB or smaller.");
    event.target.value = "";
    return;
  }
  const button = $("#reviewScriptChoose");
  button.disabled = true;
  button.textContent = "Attaching...";
  try {
    const result = await api(`/api/projects/${state.project.id}/script`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) },
      body: file,
    });
    state.project.meta.script_name = result.script_name;
    state.project.meta.script_char_count = result.script_char_count;
    updateAssistantScriptStatus(state.project.meta);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = state.project?.meta?.script_name ? "Replace script" : "Attach PDF/DOCX";
    event.target.value = "";
  }
});

$("#reviewScriptRemove").addEventListener("click", async () => {
  if (!state.project || !await confirmAction(
    "Remove the script reference from this project?",
    { title: "Remove script?", confirmLabel: "Remove script" },
  )) return;
  try {
    await api(`/api/projects/${state.project.id}/script`, { method: "DELETE" });
    delete state.project.meta.script_name;
    delete state.project.meta.script_char_count;
    updateAssistantScriptStatus(state.project.meta);
  } catch (error) {
    toast(error.message);
  }
});

function saveAssistantMessages() {
  if (!state.project) return;
  try {
    const messages = state.assistantMessages.slice(-40).map((message, index, all) => ({
      ...message,
      images: all.length - index <= 8 ? (message.images || []) : [],
    }));
    localStorage.setItem(`video-qc-assistant:${state.project.id}`, JSON.stringify(messages));
  } catch {
    $("#assistantStatus").textContent = "Chat history could not be saved in this browser.";
  }
}

async function refreshAssistantIssues() {
  const projectId = state.project?.id;
  if (!projectId) return;
  const data = await api(`/api/projects/${projectId}`);
  state.project.review = data.review;
  state.project.issues = data.issues;
  state.review = Object.assign(
    { reviewer: "", summary: "", status: {}, manual: [], checklist: {}, overrides: {} },
    data.review,
  );
  state.issues = data.issues;
  state.reviewHistory = state.reviewHistory.slice(0, state.reviewHistoryIndex + 1);
  state.reviewHistory.push({ review: structuredClone(state.review), issues: structuredClone(state.issues) });
  state.reviewHistoryIndex = state.reviewHistory.length - 1;
  state.reviewHistoryGroup = null;
  updateHistoryButtons();
  $("#filterCategory").value = "";
  $("#filterSeverity").value = "";
  $("#filterStatus").value = "active";
  renderIssues();
  renderTimeline();
}

$("#assistantAttach").addEventListener("click", () => $("#assistantImageInput").click());

$("#assistantImageInput").addEventListener("change", async (event) => {
  const input = event.currentTarget;
  const files = Array.from(input.files || []);
  input.value = "";
  const remaining = 4 - state.assistantAttachments.length;
  if (files.length > remaining) {
    $("#assistantStatus").textContent = "Attach up to 4 images per message.";
    return;
  }
  if (!files.length) return;

  state.assistantPreparingImages = true;
  $("#assistantSend").disabled = true;
  $("#assistantAttach").disabled = true;
  $("#assistantStatus").textContent = "Preparing image attachment(s)...";
  try {
    for (const file of files) {
      const prepared = await prepareAssistantImage(file);
      const preparedBytes = (prepared.data.length - "data:image/jpeg;base64,".length) * 3 / 4;
      const currentBytes = state.assistantAttachments.reduce(
        (total, image) => total + (image.data.length - "data:image/jpeg;base64,".length) * 3 / 4,
        0,
      );
      if (currentBytes + preparedBytes > 12 * 1024 * 1024) {
        throw new Error("Attached images must total no more than 12 MB.");
      }
      state.assistantAttachments.push(prepared);
      renderAssistantAttachments();
    }
    $("#assistantStatus").textContent = "Image(s) ready. A local vision model is required to inspect them.";
  } catch (error) {
    $("#assistantStatus").textContent = error.message;
  } finally {
    state.assistantPreparingImages = false;
    $("#assistantSend").disabled = state.assistantThinking;
    $("#assistantAttach").disabled = state.assistantThinking;
  }
});

$("#assistantAttachments").addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-image]");
  if (!button) return;
  state.assistantAttachments.splice(Number(button.dataset.removeImage), 1);
  renderAssistantAttachments();
});

$("#assistantIssueContextClear").addEventListener("click", () => setAssistantIssueContext(null));

$("#assistantInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229 && !state.assistantThinking) {
    event.preventDefault();
    $("#assistantForm").requestSubmit();
  }
});

$("#assistantForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.project || state.assistantThinking || state.assistantPreparingImages) return;
  const input = $("#assistantInput");
  const message = input.value.trim();
  const attachments = state.assistantAttachments.slice();
  if (!message && !attachments.length) return;

  const history = state.assistantMessages.slice(-8);
  state.assistantMessages.push({
    role: "user",
    content: message || "Please review the attached image(s).",
    images: attachments.map((image) => image.preview),
  });
  state.assistantThinking = true;
  state.assistantAttachments = [];
  input.value = "";
  renderAssistantAttachments();
  $("#assistantSend").disabled = true;
  $("#assistantAttach").disabled = true;
  $("#assistantStatus").textContent = attachments.length
    ? "Reviewing your image(s) with the local vision model..."
    : "The assistant is preparing a response. First use may take longer while the local model loads.";
  renderAssistantMessages();
  saveAssistantMessages();
  try {
    if (state.pendingReviewSave) {
      await flushSave();
      if (state.pendingReviewSave) {
        throw new Error("Your review changes could not be saved. Retry after the save succeeds.");
      }
    }
    const result = await api(`/api/projects/${state.project.id}/assistant`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        history,
        time: player.currentTime || 0,
        images: attachments.map((image) => image.data),
        issue_id: state.assistantContextIssueId,
      }),
    });
    state.assistantMessages.push({ role: "assistant", content: result.answer });
    state.assistantMessages = state.assistantMessages.slice(-40);
    setAssistantIssueContext(null);
    if (result.history_warning) toast(result.history_warning);
    saveAssistantMessages();
    renderAssistantMessages();
    if (result.issues_updated) {
      try {
        await refreshAssistantIssues();
      } catch (error) {
        toast(`The findings changed, but the issue list could not be refreshed: ${error.message}`);
      }
    }
    $("#assistantStatus").textContent = "Local chat. Your video and conversation stay on this PC.";
  } catch (error) {
    $("#assistantStatus").textContent = error.message;
  } finally {
    state.assistantThinking = false;
    renderAssistantMessages();
    $("#assistantSend").disabled = false;
    $("#assistantAttach").disabled = false;
    input.focus();
  }
});

$("#assistantClear").addEventListener("click", async () => {
  if (!state.project) return;
  if (!await confirmAction(
    "This removes the saved conversation from this project. This cannot be undone.",
    { title: "Clear chat?", confirmLabel: "Clear conversation" },
  )) return;
  try {
    await api(`/api/projects/${state.project.id}/assistant-history`, { method: "DELETE" });
  } catch (error) {
    toast(error.message);
    return;
  }
  state.assistantMessages = [];
  setAssistantIssueContext(null);
  saveAssistantMessages();
  renderAssistantMessages();
  $("#assistantStatus").textContent = "Conversation cleared.";
  toast("Chat cleared.");
});

$("#transcriptList").addEventListener("click", (e) => {
  const p = e.target.closest("[data-seek]");
  if (p) seek(parseFloat(p.dataset.seek));
});

$("#reviewer").addEventListener("input", (e) => { state.review.reviewer = e.target.value; saveReview("reviewer"); });
$("#summary").addEventListener("input", (e) => { state.review.summary = e.target.value; saveReview("summary"); });

$("#dedupeBtn").addEventListener("click", async () => {
  if (!state.project) return;
  const button = $("#dedupeBtn");
  button.disabled = true;
  button.textContent = "Reviewing...";
  $("#dedupeStatus").textContent = "Loading the local semantic model and reviewing open findings...";
  $("#dedupeResults").innerHTML = "";
  try {
    const result = await api(`/api/projects/${state.project.id}/duplicate-review`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
    });
    state.duplicateReviewHidden = false;
    renderDuplicateGroups(result.groups || []);
    if (!result.groups?.length) {
      $("#dedupeStatus").textContent = result.source === "exact"
        ? `No exact repeats found. ${result.warning || "Semantic model unavailable."}`
        : "No semantically similar open findings were found.";
    } else if (result.source === "exact") {
      $("#dedupeStatus").textContent = `Semantic model unavailable; showing ${result.groups.length} exact-match group${result.groups.length === 1 ? "" : "s"}. ${result.warning || "Check each before dismissing."}`;
    } else {
      $("#dedupeStatus").textContent = `Local semantic model proposed ${result.groups.length} group${result.groups.length === 1 ? "" : "s"}; check each before dismissing.`;
    }
  } catch (error) {
    $("#dedupeStatus").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Review repeated issues";
  }
});

$("#dedupeResults").addEventListener("click", (event) => {
  const button = event.target.closest("[data-dismiss-repeats], [data-keep-repeats]");
  if (!button) return;
  const index = Number(button.dataset.dismissRepeats ?? button.dataset.keepRepeats);
  const group = state.duplicateGroups[index];
  if (!group) return;
  if (button.dataset.keepRepeats !== undefined) {
    renderDuplicateGroups(state.duplicateGroups.filter((_, groupIndex) => groupIndex !== index));
    $("#dedupeStatus").textContent = "Proposal ignored; no issues were changed.";
    return;
  }
  const remaining = state.duplicateGroups.filter((_, groupIndex) => groupIndex !== index);
  const result = dismissDuplicateGroups([group]);
  renderDuplicateGroups(remaining);
  $("#dedupeStatus").textContent = result.dismissedCount
    ? `Dismissed ${result.dismissedCount} repeat(s); their timecodes were added to the kept issue.`
    : "No open repeats remained in this proposal.";
});

$("#dedupeDismissAll").addEventListener("click", async () => {
  const groups = state.duplicateGroups;
  const repeatCount = groups.reduce((total, group) => total + group.dismiss.filter((id) =>
    state.issues.some((issue) => issue.id === id && issue.status === "open")).length, 0);
  if (!repeatCount) return;
  if (!await confirmAction(
    `Dismiss all ${repeatCount} proposed repeats across ${groups.length} groups? Kept issues will retain the repeated timecodes.`,
    { title: "Dismiss repeated findings?", confirmLabel: "Dismiss repeats", danger: false },
  )) return;
  const result = dismissDuplicateGroups(groups);
  renderDuplicateGroups([]);
  $("#dedupeStatus").textContent = `Dismissed ${result.dismissedCount} repeats across ${result.groupCount} groups; timecodes were added to the kept issues.`;
});

$("#dedupeCloseReview").addEventListener("click", () => {
  if (!state.duplicateGroups.length) return;
  state.duplicateReviewHidden = !state.duplicateReviewHidden;
  renderDuplicateGroups(state.duplicateGroups);
  $("#dedupeStatus").textContent = state.duplicateReviewHidden
    ? "Repeat review closed. Your proposals are saved here until you reopen them or leave this project."
    : "Repeat review reopened.";
});

// ---- tabs
function switchTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel").forEach((p) => (p.hidden = p.dataset.panel !== name));
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => {
  switchTab(b.dataset.tab);
}));

// ---- playback sync
let lastNowKey = "";
player.addEventListener("timeupdate", () => {
  if (!state.project) return;
  const t = player.currentTime;
  $("#clock").textContent = timecode(t);
  $("#clockReal").textContent = realTime(t);
  $("#playhead").style.left = `${(t / state.duration) * 100}%`;

  const now = state.issues.filter((i) => i.status !== "dismissed" &&
    t >= i.time - 0.25 && t <= (i.end ?? i.time + 1.5));
  const key = now.map((i) => i.id).join(",");
  if (key !== lastNowKey) {
    lastNowKey = key;
    $("#nowIssues").innerHTML = now.map((i) =>
      `<span class="chip" style="border-color:var(--${sevOf(i) === "manual" ? "manual" : sevOf(i)})">${esc(i.title)}</span>`).join("");
    $$(".issue.now").forEach((c) => c.classList.remove("now"));
    now.forEach((i) => $(`.issue[data-id="${CSS.escape(i.id)}"]`)?.classList.add("now"));
  }

  const segs = state.project.analysis.transcript || [];
  const n = segs.findIndex((s) => t >= s.start && t < s.end);
  $$(".transcript p.now").forEach((p) => p.classList.remove("now"));
  const p = n >= 0 ? $(`.transcript p[data-n="${n}"]`) : null;
  if (p) {
    p.classList.add("now");
    if (!player.paused && !$('[data-panel="transcript"]').hidden) p.scrollIntoView({ block: "nearest" });
  }
});

document.addEventListener("keydown", (e) => {
  if ($("#reviewView").hidden || ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
  const frame = 1 / state.fps;
  const actions = {
    " ": () => (player.paused ? player.play() : player.pause()),
    ArrowLeft: () => (player.currentTime -= 1),
    ArrowRight: () => (player.currentTime += 1),
    ",": () => { player.pause(); player.currentTime -= frame; },
    ".": () => { player.pause(); player.currentTime += frame; },
    n: () => { player.pause(); $("#addText").focus(); },
  };
  const fn = actions[e.key] || actions[e.key.toLowerCase()];
  if (fn) { e.preventDefault(); fn(); }
});

$("#exportBtn").addEventListener("click", async () => {
  await flushSave();
  window.location.href = `/api/projects/${state.project.id}/export.docx`;
});

$("#homeBtn").addEventListener("click", async () => { await flushSave(); loadHome(); });

// ---- start
const startId = new URLSearchParams(location.search).get("p");
if (startId) openProject(startId).catch(loadHome);
else loadHome();
