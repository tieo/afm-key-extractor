// Entry point: bootstrap → fetch status → open SSE.

import {
  initSSE,
  selectView,
  updatePipeline,
  updateStatusBadge,
  updateRunningView,
  updateKeysPanel,
  updateErrorBanner,
} from "./state.js";
import {
  wireButtons,
  setVncConfig,
  ensureVncLoaded,
  setSmsPhone,
  updateAbortButton,
} from "./vm-panel.js";
import { checkSetupStatus, wireSetupButtons } from "./setup-wizard.js";
import { get } from "./api.js";

const MAX_LOG_ENTRIES = 20;

// Cached setup status — needed by view selection in SSE updates.
let _setupStatus = { basesystem_ready: false, golden_image_ready: false };

// ---------------------------------------------------------------------------
// Log panel
// ---------------------------------------------------------------------------

function appendLogEntry(entry) {
  const panel = document.getElementById("log-panel");
  if (!panel) return;

  const row = document.createElement("div");
  row.className = `log-entry ${entry.level || ""}`;

  const ts = new Date(entry.ts);
  const timeStr = ts.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  row.innerHTML =
    `<span class="log-ts">${timeStr}</span>` +
    `<span class="log-cat">${entry.cat || ""}</span>` +
    `<span class="log-msg">${entry.msg || ""}</span>`;

  panel.appendChild(row);
  while (panel.children.length > MAX_LOG_ENTRIES) panel.removeChild(panel.firstChild);
  panel.scrollTop = panel.scrollHeight;
}

async function loadInitialLog() {
  const entries = await get("/api/log");
  if (!Array.isArray(entries)) return;
  const panel = document.getElementById("log-panel");
  if (panel) panel.innerHTML = "";
  entries.slice(-MAX_LOG_ENTRIES).forEach(appendLogEntry);
}

// ---------------------------------------------------------------------------
// Full UI update (called on init and after each SSE state event)
// ---------------------------------------------------------------------------

async function applyStatus(status) {
  selectView(status, _setupStatus);
  updatePipeline(status, _setupStatus);
  updateStatusBadge(status);
  updateAbortButton(status.running);
  updateRunningView(status);
  updateErrorBanner(status);

  // Ensure VNC iframe is loaded when visible.
  const vncSection = document.getElementById("vnc-section");
  if (vncSection && vncSection.style.display !== "none") {
    ensureVncLoaded();
  }
}

async function refreshKeys() {
  const keys = await get("/api/keys/");
  updateKeysPanel(Array.isArray(keys) ? keys : []);
}

// ---------------------------------------------------------------------------
// SSE event handler
// ---------------------------------------------------------------------------

function onSseEvent(event) {
  if (event.type === "state") {
    const status = {
      flow: event.flow ?? null,
      state: event.state,
      label: event.label ?? event.state,
      error: event.error ?? null,
      running: event.state !== "idle" && event.state !== "done" && event.state !== "error",
    };
    applyStatus(status);

    if (event.state === "done" || event.state === "idle") {
      // Re-check setup status (install creates golden image) then re-apply view.
      checkSetupStatus().then(async (s) => {
        if (s) _setupStatus = s;
        await applyStatus(status);
        await refreshKeys();
      });
      return; // applyStatus will be called above once setupStatus is fresh
    }
  } else if (event.type === "log") {
    appendLogEntry(event);
  } else if (event.type === "sms_phone") {
    setSmsPhone(event.phone);
  }
}

// ---------------------------------------------------------------------------
// Periodic refresh fallback
// ---------------------------------------------------------------------------

async function refreshStatus() {
  const data = await get("/api/automation/status");
  if (data) applyStatus(data);
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", async () => {
  wireButtons();
  wireSetupButtons();

  // Fetch server config first (VNC URL, VM enabled flag).
  const vmConfig = await get("/api/config");
  if (vmConfig) {
    setVncConfig(vmConfig);
  }

  // Setup status determines which view to show (setup / install / ready).
  const setupStatus = await checkSetupStatus();
  if (setupStatus) _setupStatus = setupStatus;

  // Initial automation status.
  const status = await get("/api/automation/status") ?? {
    flow: null, state: "idle", label: "Idle", error: null, running: false,
  };

  await applyStatus(status);

  // If already running, pre-load VNC.
  const vncSection = document.getElementById("vnc-section");
  if (vncSection && vncSection.style.display !== "none") ensureVncLoaded();

  // Load keys panel.
  await refreshKeys();

  // Load recent log entries.
  await loadInitialLog();

  // setup-wizard fires this when a download completes.
  window.addEventListener("setup-complete", async () => {
    const s = await checkSetupStatus();
    if (s) _setupStatus = s;
    await applyStatus(await get("/api/automation/status") ?? {
      flow: null, state: "idle", label: "Idle", error: null, running: false,
    });
  });

  // Open SSE stream.
  initSSE(onSseEvent);

  // Safety-net refresh every 15 s.
  setInterval(refreshStatus, 15000);
});
