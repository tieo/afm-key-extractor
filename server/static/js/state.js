import { updateOpenVmButton } from "./vm-panel.js";

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

export function labelFor(flow, state) {
  if (flow === "install") return INSTALL_LABELS[state] ?? state;
  if (flow === "runtime") return RUNTIME_LABELS[state] ?? state;
  return state;
}

// ---------------------------------------------------------------------------
// View selection
// ---------------------------------------------------------------------------

const _ALL_VIEWS = ["view-setup", "view-install", "view-running", "view-ready"];

/**
 * Switch to the appropriate view based on automation + setup state.
 * @param {{ running: boolean, state: string }} status
 * @param {{ basesystem_ready: boolean, golden_image_ready: boolean } | null} setupStatus
 */
export function selectView(status, setupStatus) {
  let target;
  if (status.running) {
    target = "view-running";
  } else if (!setupStatus || !setupStatus.basesystem_ready) {
    target = "view-setup";
  } else if (!setupStatus.golden_image_ready) {
    target = "view-install";
  } else {
    target = "view-ready";
  }

  _ALL_VIEWS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.style.display = id === target ? "" : "none";
  });

  // Header chrome that's view-dependent.
  updateOpenVmButton(target);
}

// ---------------------------------------------------------------------------
// Pipeline overview (macro phases: recovery image → golden image → extract)
// ---------------------------------------------------------------------------

/**
 * Highlight the current macro phase and mark earlier ones done.
 * Phase 0 = recovery image, 1 = golden image, 2 = extract keys.
 * @param {{ running: boolean, flow: string | null }} status
 * @param {{ basesystem_ready: boolean, golden_image_ready: boolean } | null} setupStatus
 */
export function updatePipeline(status, setupStatus) {
  const base = !!setupStatus?.basesystem_ready;
  const golden = !!setupStatus?.golden_image_ready;

  let current;
  if (status.running && status.flow === "install") current = 1;
  else if (status.running && status.flow === "runtime") current = 2;
  else if (!base) current = 0;
  else if (!golden) current = 1;
  else current = 2;

  document.querySelectorAll(".pipeline-step").forEach((el) => {
    const phase = Number(el.dataset.phase);
    el.classList.toggle("pipeline-step--done", phase < current);
    el.classList.toggle("pipeline-step--current", phase === current);
    el.classList.toggle("pipeline-step--pending", phase > current);
    // Clear progress fill on phases that aren't currently active.
    if (phase !== current) el.style.removeProperty("--phase-progress");
  });
}

/** Set a sub-progress percentage on a specific pipeline phase.
 *  The current phase's background fills left-to-right based on this. */
export function setPipelineProgress(phase, pct) {
  const el = document.querySelector(`.pipeline-step[data-phase="${phase}"]`);
  if (el) el.style.setProperty("--phase-progress", `${Math.max(0, Math.min(100, pct))}%`);
}

// ---------------------------------------------------------------------------
// Status badge
// ---------------------------------------------------------------------------

export function updateStatusBadge(status) {
  const badge = document.getElementById("status-badge");
  if (!badge) return;
  if (status.running) {
    badge.className = "badge badge-running";
    badge.textContent = "Running";
  } else if (status.state === "error") {
    badge.className = "badge badge-error";
    badge.textContent = "Error";
  } else if (status.state === "done") {
    badge.className = "badge badge-done";
    badge.textContent = "Done";
  } else {
    badge.className = "badge badge-idle";
    badge.textContent = "Idle";
  }
}

// ---------------------------------------------------------------------------
// Stage bar (shown inside view-running)
// ---------------------------------------------------------------------------

const VNC_VISIBLE_STATES = new Set([
  "booting_picker", "picker_selecting", "waiting_recovery",
  "format_disk", "waiting_format_done", "reinstall_clicking",
  "waiting_install", "booting_installed",
  "sa_country", "sa_languages", "sa_accessibility", "sa_data_privacy",
  "sa_migration", "sa_apple_id", "sa_terms", "sa_create_account",
  "sa_apple_id_2", "sa_terms_2", "sa_location", "sa_timezone",
  "sa_analytics", "sa_screen_time", "sa_appearance",
  "dismiss_first_boot", "shutting_down", "baking_golden",
  "restoring_golden", "booting", "picker_selecting",
  "waiting_login_screen", "logging_in", "waiting_desktop",
  "disabling_sleep", "opening_apple_id", "typing_credentials",
  "waiting_2fa_or_signed_in", "awaiting_2fa", "typing_2fa",
  "waiting_signed_in", "dismissing_post_signin",
  "resolving_apple_id_update", "enabling_find_my",
  "waiting_icloud_sync", "extracting_keys",
]);

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

export function updateRunningView(status) {
  const { flow, state, label, running } = status;

  // Stage bar.
  const stageBar = document.getElementById("stage-bar");
  const stageLabelEl = document.getElementById("stage-label");
  if (stageBar && stageLabelEl) {
    const stages = flow === "install" ? INSTALL_STAGES : flow === "runtime" ? RUNTIME_STAGES : [];
    const activeIdx = stages.indexOf(state);
    stageBar.innerHTML = flow === "install"
      ? _renderInstallBar(state)
      : stages.map((s, i) => {
          let cls = "stage";
          if (i < activeIdx) cls += " stage--done";
          else if (i === activeIdx) cls += " stage--active";
          return `<span class="${cls}">${labelFor(flow, s)}</span>`;
        }).join("");
    stageLabelEl.textContent = label || labelFor(flow, state);
  }

  // VNC iframe — show for active automation states.
  const vncSection = document.getElementById("vnc-section");
  if (vncSection) {
    vncSection.style.display = (running && VNC_VISIBLE_STATES.has(state)) ? "" : "none";
  }

  // 2FA form.
  const twofaForm = document.getElementById("twofa-form");
  if (twofaForm) {
    const show2fa = running && state === "awaiting_2fa";
    twofaForm.style.display = show2fa ? "" : "none";
    if (show2fa) {
      const inp = document.getElementById("twofa-code");
      if (inp && document.activeElement !== inp) inp.focus();
    }
  }
}

// ---------------------------------------------------------------------------
// Keys panel (shown inside view-ready)
// ---------------------------------------------------------------------------

let _allKeys = [];

export function updateKeysPanel(keys) {
  const panel = document.getElementById("keys-panel");
  if (!panel) return;
  if (!Array.isArray(keys) || keys.length === 0) {
    panel.style.display = "none";
    _allKeys = [];
    return;
  }
  panel.style.display = "";
  _allKeys = keys;

  const metaEl = document.getElementById("keys-meta");
  if (metaEl) {
    const lastRun = new Date(keys[0].mtime);
    const dateStr = lastRun.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    const noun = keys.length === 1 ? "key" : "keys";
    metaEl.textContent = `${keys.length} ${noun} · Last extracted ${dateStr}`;
  }

  const listEl = document.getElementById("keys-list");
  if (listEl) {
    listEl.innerHTML =
      `<div class="key-row key-row--header">
        <label class="key-checkbox-wrap" title="Select all">
          <input type="checkbox" id="keys-select-all" class="key-checkbox">
        </label>
        <span class="key-col-name">Name</span>
        <span class="key-col-date">Extracted</span>
      </div>` +
      keys.map((k) => {
        const d = new Date(k.mtime);
        const dateStr = d.toLocaleString([], { dateStyle: "short", timeStyle: "short" });
        const id = k.name.replace(/\.json$/, "");
        return `<label class="key-row" data-filename="${k.name}">
          <span class="key-checkbox-wrap">
            <input type="checkbox" class="key-checkbox key-item-cb" data-filename="${k.name}">
          </span>
          <span class="key-name">${id}</span>
          <span class="key-date">${dateStr}</span>
        </label>`;
      }).join("");

    // Wire select-all toggle.
    const selectAll = listEl.querySelector("#keys-select-all");
    if (selectAll) {
      selectAll.addEventListener("change", () => {
        listEl.querySelectorAll(".key-item-cb").forEach((cb) => { cb.checked = selectAll.checked; });
        _updateDownloadButton();
      });
    }
    listEl.querySelectorAll(".key-item-cb").forEach((cb) => {
      cb.addEventListener("change", () => {
        const all = listEl.querySelectorAll(".key-item-cb");
        const checked = listEl.querySelectorAll(".key-item-cb:checked");
        const selectAllCb = listEl.querySelector("#keys-select-all");
        if (selectAllCb) {
          selectAllCb.checked = checked.length === all.length;
          selectAllCb.indeterminate = checked.length > 0 && checked.length < all.length;
        }
        _updateDownloadButton();
      });
    });
  }

  _updateDownloadButton();
}

function _updateDownloadButton() {
  const btn = document.getElementById("btn-download-keys");
  if (!btn) return;
  const checked = document.querySelectorAll(".key-item-cb:checked");
  if (checked.length > 0 && checked.length < _allKeys.length) {
    btn.textContent = `Download Selected (${checked.length})`;
    btn.onclick = (e) => {
      e.preventDefault();
      const params = new URLSearchParams();
      checked.forEach((cb) => params.append("include", cb.dataset.filename));
      _triggerDownload(`/api/keys/zip?${params}`, "airtag-keys-selected.zip");
    };
  } else {
    btn.textContent = "Download ZIP";
    btn.onclick = null;
    btn.href = "/api/keys/zip";
  }
}

function _triggerDownload(url, filename) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ---------------------------------------------------------------------------
// Error banner
// ---------------------------------------------------------------------------

export function updateErrorBanner(status) {
  const banner = document.getElementById("error-banner");
  if (!banner) return;
  if (status.error && status.state === "error") {
    banner.textContent = "Error: " + status.error;
    banner.style.display = "";
  } else {
    banner.style.display = "none";
  }
}

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------

let _es = null;

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
    console.warn("SSE connection error, browser will retry");
  };
}
