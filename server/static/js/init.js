// Entry point: DOMContentLoaded → fetch initial status → initSSE().

import { initSSE, updateUI, fetchInitialStatus } from "./state.js";
import { wireButtons, setVncPort, ensureVncLoaded, setSmsPhone } from "./vm-panel.js";
import { checkSetupStatus, wireSetupButtons } from "./setup-wizard.js";
import { get } from "./api.js";

// Maximum number of log entries to show in the UI.
const MAX_LOG_ENTRIES = 20;

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

  // Keep only the last MAX_LOG_ENTRIES rows.
  while (panel.children.length > MAX_LOG_ENTRIES) {
    panel.removeChild(panel.firstChild);
  }

  // Auto-scroll to bottom.
  panel.scrollTop = panel.scrollHeight;
}

async function loadInitialLog() {
  const entries = await get("/api/log");
  if (!Array.isArray(entries)) return;
  // Show only the last MAX_LOG_ENTRIES.
  const slice = entries.slice(-MAX_LOG_ENTRIES);
  const panel = document.getElementById("log-panel");
  if (panel) panel.innerHTML = "";
  slice.forEach(appendLogEntry);
}

// ---------------------------------------------------------------------------
// SSE event handler
// ---------------------------------------------------------------------------

function onSseEvent(event) {
  if (event.type === "state") {
    // Map SSE state event to the updateUI contract.
    updateUI({
      flow: event.flow ?? null,
      state: event.state,
      label: event.label ?? event.state,
      error: event.error ?? null,
      running: event.state !== "idle" && event.state !== "done" && event.state !== "error",
    });

    // Show/hide noVNC when state changes.
    const vncSection = document.getElementById("vnc-section");
    if (vncSection && vncSection.style.display !== "none") {
      ensureVncLoaded();
    }
    // After install finishes, refresh setup status to reveal the runtime card.
    if (event.state === "done" || event.state === "idle") {
      checkSetupStatus();
      refreshDownloadButton();
    }
  } else if (event.type === "log") {
    appendLogEntry(event);
  } else if (event.type === "sms_phone") {
    setSmsPhone(event.phone);
  } else if (event.type === "2fa_required") {
    // Engine can emit this explicitly — updateUI handles the state-based show/hide,
    // but in case the type is sent directly, trigger a status refresh.
    refreshStatus();
  }
}

// ---------------------------------------------------------------------------
// Periodic status refresh (fallback for reconnected clients)
// ---------------------------------------------------------------------------

async function refreshStatus() {
  const data = await get("/api/automation/status");
  if (data) updateUI(data);
}

async function refreshDownloadButton() {
  const keys = await get("/api/keys/");
  const btn = document.getElementById("btn-download-keys");
  if (btn) btn.style.display = Array.isArray(keys) && keys.length ? "" : "none";
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", async () => {
  wireButtons();
  wireSetupButtons();

  // Check setup status first — hides install/runtime cards if BaseSystem missing.
  await checkSetupStatus();

  // Load initial automation status.
  const initial = await fetchInitialStatus();

  // Set VNC port from vm/status (fetchInitialStatus updates window.VNC_WS_PORT).
  setVncPort(window.VNC_WS_PORT);

  if (initial) {
    updateUI(initial);

    // If already running, pre-load the VNC iframe.
    const vncSection = document.getElementById("vnc-section");
    if (vncSection && vncSection.style.display !== "none") {
      ensureVncLoaded();
    }
  }

  await refreshDownloadButton();

  // Load the last log entries.
  await loadInitialLog();

  // Open SSE stream.
  initSSE(onSseEvent);

  // Refresh status every 15 seconds as a safety net (SSE may miss events on reconnect).
  setInterval(refreshStatus, 15000);
});
