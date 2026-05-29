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

// Stage labels come from the backend (states.py) via /api/automation/labels.
// loadLabels() is called once at init; labelFor() reads from the cached maps.
// Single source of truth - the previous dual-dict approach drifted (the JS
// said "Installing macOS (20-45 min)" while the Python said "60-90 min").
let _LABELS = { install: {}, runtime: {} };

export async function loadLabels() {
  try {
    const r = await fetch("/api/automation/labels");
    if (r.ok) _LABELS = await r.json();
  } catch (e) {
    /* fall back to raw state names */
  }
}

export function labelFor(flow, state) {
  return _LABELS[flow]?.[state] ?? state;
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
    return `<span class="${cls}">${labelFor("install", s)}</span>`;
  }).join("");
}

export function updateRunningView(status) {
  const { flow, state, label, running } = status;

  // Stage bar. (No separate stage-label below it - the active pill already
  // shows the current label, the duplicate title was redundant.)
  const stageBar = document.getElementById("stage-bar");
  if (stageBar) {
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
