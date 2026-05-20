// Drives all UI from SSE events + initial GET /api/automation/status.

import { get } from "./api.js";

// Stage sequences — values match the enum .value strings in states.py.
export const INSTALL_STAGES = [
  "idle",
  "booting_picker",
  "picker_selecting",
  "waiting_recovery",
  "format_disk",
  "waiting_format_done",
  "reinstall_clicking",
  "waiting_install",
  "booting_installed",
  "sa_country",
  "sa_languages",
  "sa_accessibility",
  "sa_data_privacy",
  "sa_migration",
  "sa_apple_id",
  "sa_terms",
  "sa_create_account",
  "sa_apple_id_2",
  "sa_terms_2",
  "sa_location",
  "sa_timezone",
  "sa_analytics",
  "sa_screen_time",
  "sa_appearance",
  "dismiss_first_boot",
  "shutting_down",
  "baking_golden",
  "done",
];

export const RUNTIME_STAGES = [
  "idle",
  "restoring_golden",
  "booting",
  "picker_selecting",
  "waiting_login_screen",
  "logging_in",
  "waiting_desktop",
  "disabling_sleep",
  "opening_apple_id",
  "typing_credentials",
  "waiting_2fa_or_signed_in",
  "awaiting_2fa",
  "typing_2fa",
  "waiting_signed_in",
  "dismissing_post_signin",
  "resolving_apple_id_update",
  "enabling_find_my",
  "waiting_icloud_sync",
  "extracting_keys",
  "shutting_down",
  "done",
];

// SA stages collapsed to one display entry in the install stage bar.
const _SA_STAGES = [
  "sa_country", "sa_languages", "sa_accessibility", "sa_data_privacy",
  "sa_migration", "sa_apple_id", "sa_terms", "sa_create_account",
  "sa_apple_id_2", "sa_terms_2", "sa_location", "sa_timezone",
  "sa_analytics", "sa_screen_time", "sa_appearance",
];

const _INSTALL_BAR = [
  "idle", "booting_picker", "picker_selecting", "waiting_recovery",
  "format_disk", "waiting_format_done", "reinstall_clicking",
  "waiting_install", "booting_installed",
  "__sa__",
  "dismiss_first_boot", "shutting_down", "baking_golden", "done",
];

function _renderInstallBar(state) {
  const realIdx = INSTALL_STAGES.indexOf(state);
  const saEnd = INSTALL_STAGES.indexOf("sa_appearance");
  const saSubIdx = _SA_STAGES.indexOf(state);

  return _INSTALL_BAR.map((s) => {
    if (s === "__sa__") {
      let cls = "stage";
      let lbl = "Setup Assistant";
      if (realIdx > saEnd) cls += " stage--done";
      else if (saSubIdx !== -1) { cls += " stage--active"; lbl += ` (${saSubIdx + 1}/15)`; }
      return `<span class="${cls}">${lbl}</span>`;
    }
    const idx = INSTALL_STAGES.indexOf(s);
    let cls = "stage";
    if (realIdx > idx) cls += " stage--done";
    else if (realIdx === idx) cls += " stage--active";
    return `<span class="${cls}">${INSTALL_LABELS[s] ?? s}</span>`;
  }).join("");
}

// Human-readable labels for each stage (mirrors INSTALL/RUNTIME_STAGE_LABELS in states.py).
const INSTALL_LABELS = {
  idle: "Idle",
  booting_picker: "Starting VM",
  picker_selecting: "Selecting installer",
  waiting_recovery: "Loading Recovery",
  format_disk: "Formatting disk",
  waiting_format_done: "Formatting disk",
  reinstall_clicking: "Starting installer",
  waiting_install: "Installing macOS (20–45 min)",
  booting_installed: "Rebooting",
  sa_country: "Setup Assistant (1/15)",
  sa_languages: "Setup Assistant (2/15)",
  sa_accessibility: "Setup Assistant (3/15)",
  sa_data_privacy: "Setup Assistant (4/15)",
  sa_migration: "Setup Assistant (5/15)",
  sa_apple_id: "Setup Assistant (6/15)",
  sa_terms: "Setup Assistant (7/15)",
  sa_create_account: "Setup Assistant (8/15)",
  sa_apple_id_2: "Setup Assistant (9/15)",
  sa_terms_2: "Setup Assistant (10/15)",
  sa_location: "Setup Assistant (11/15)",
  sa_timezone: "Setup Assistant (12/15)",
  sa_analytics: "Setup Assistant (13/15)",
  sa_screen_time: "Setup Assistant (14/15)",
  sa_appearance: "Setup Assistant (15/15)",
  dismiss_first_boot: "Finalising",
  shutting_down: "Shutting down VM",
  baking_golden: "Saving image",
  done: "Installation complete",
  error: "Error",
};

const RUNTIME_LABELS = {
  idle: "Idle",
  restoring_golden: "Restoring VM image",
  booting: "Starting VM",
  picker_selecting: "Selecting macOS",
  waiting_login_screen: "Waiting for login screen",
  logging_in: "Logging in",
  waiting_desktop: "Waiting for desktop",
  disabling_sleep: "Configuring VM",
  opening_apple_id: "Opening Apple ID settings",
  typing_credentials: "Entering Apple ID",
  waiting_2fa_or_signed_in: "Waiting for Apple response",
  awaiting_2fa: "Waiting for your 2FA code",
  typing_2fa: "Submitting 2FA code",
  waiting_signed_in: "Completing sign-in",
  dismissing_post_signin: "Dismissing prompts",
  resolving_apple_id_update: "Updating Apple ID",
  enabling_find_my: "Enabling Find My",
  waiting_icloud_sync: "Waiting for iCloud sync",
  extracting_keys: "Extracting AirTag keys",
  shutting_down: "Shutting down VM",
  done: "Keys extracted",
  error: "Error",
};

/**
 * Returns human-readable label for a given flow + state combo.
 */
export function labelFor(flow, state) {
  if (flow === "install") return INSTALL_LABELS[state] ?? state;
  if (flow === "runtime") return RUNTIME_LABELS[state] ?? state;
  return state;
}

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------

let _es = null;

/**
 * Open an EventSource to /api/events. Calls updateUI on every "state" event.
 */
export function initSSE(onEvent) {
  if (_es) { _es.close(); }
  _es = new EventSource("/api/events");
  _es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      onEvent(data);
    } catch (e) {
      console.warn("SSE parse error", e);
    }
  };
  _es.onerror = () => {
    // Browser will reconnect automatically — no explicit retry needed.
    console.warn("SSE connection error, browser will retry");
  };
}

// ---------------------------------------------------------------------------
// UI update
// ---------------------------------------------------------------------------

/**
 * Render the stage bar and label from a status/event object.
 * @param {{ flow: string|null, state: string, label: string, error: string|null, running: boolean }} status
 */
export function updateUI(status) {
  const { flow, state, label, error, running } = status;

  const stageSection = document.getElementById("stage-section");
  const stageBar = document.getElementById("stage-bar");
  const stageLabelEl = document.getElementById("stage-label");
  const vncSection = document.getElementById("vnc-section");
  const actionsSection = document.getElementById("actions-section");
  const errorBanner = document.getElementById("error-banner");
  const abortBtn = document.getElementById("btn-abort");

  // Show/hide abort button while running.
  if (abortBtn) abortBtn.style.display = running ? "" : "none";

  // Stage bar.
  const stages = flow === "install" ? INSTALL_STAGES : flow === "runtime" ? RUNTIME_STAGES : [];
  const activeIdx = stages.indexOf(state);

  if (flow && stages.length > 0) {
    stageSection.style.display = "";
    stageBar.innerHTML = flow === "install"
      ? _renderInstallBar(state)
      : stages.map((s, i) => {
          let cls = "stage";
          if (i < activeIdx) cls += " stage--done";
          else if (i === activeIdx) cls += " stage--active";
          return `<span class="${cls}">${labelFor(flow, s)}</span>`;
        }).join("");
    stageLabelEl.textContent = label || labelFor(flow, state);
  } else {
    stageSection.style.display = "none";
    stageBar.innerHTML = "";
    stageLabelEl.textContent = "";
  }

  // noVNC — show once the VM is booting (not idle/done/error).
  const VNC_VISIBLE_STATES = new Set([
    // install
    "booting_picker", "picker_selecting", "waiting_recovery",
    "format_disk", "waiting_format_done", "reinstall_clicking",
    "waiting_install", "booting_installed",
    "sa_country", "sa_languages", "sa_accessibility", "sa_data_privacy",
    "sa_migration", "sa_apple_id", "sa_terms", "sa_create_account",
    "sa_apple_id_2", "sa_terms_2", "sa_location", "sa_timezone",
    "sa_analytics", "sa_screen_time", "sa_appearance",
    "dismiss_first_boot", "shutting_down", "baking_golden",
    // runtime
    "restoring_golden", "booting", "picker_selecting",
    "waiting_login_screen", "logging_in", "waiting_desktop",
    "disabling_sleep", "opening_apple_id", "typing_credentials",
    "waiting_2fa_or_signed_in", "awaiting_2fa", "typing_2fa",
    "waiting_signed_in", "dismissing_post_signin",
    "resolving_apple_id_update", "enabling_find_my",
    "waiting_icloud_sync", "extracting_keys",
  ]);
  const showVnc = running && VNC_VISIBLE_STATES.has(state);
  vncSection.style.display = showVnc ? "" : "none";

  // Action buttons — hide while running, show when idle/done/error.
  const showActions = !running || state === "done" || state === "error" || !flow;
  actionsSection.style.display = showActions ? "" : "none";

  // 2FA form — shown when SSE fires "awaiting_2fa".
  const twofaForm = document.getElementById("twofa-form");
  if (twofaForm) {
    const show2fa = running && state === "awaiting_2fa";
    twofaForm.style.display = show2fa ? "" : "none";
    if (show2fa) {
      const inp = document.getElementById("twofa-code");
      if (inp && document.activeElement !== inp) inp.focus();
    }
  }

  // Error banner.
  if (error && state === "error") {
    errorBanner.textContent = "Error: " + error;
    errorBanner.style.display = "";
  } else {
    errorBanner.style.display = "none";
  }
}

// ---------------------------------------------------------------------------
// Initial status fetch
// ---------------------------------------------------------------------------

export async function fetchInitialStatus() {
  const data = await get("/api/automation/status");
  if (!data) return;
  // Fetch VNC port from vm status and set global.
  const vmData = await get("/api/vm/status");
  if (vmData && vmData.vnc_ws_port) {
    window.VNC_WS_PORT = vmData.vnc_ws_port;
  }
  return data;
}
