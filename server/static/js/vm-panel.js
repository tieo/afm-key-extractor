// Handles: 2FA form, noVNC iframe, Start Install / Start Runtime, Abort.

import { post, get } from "./api.js";

// ---------------------------------------------------------------------------
// noVNC iframe
// ---------------------------------------------------------------------------

let _vncConfig = { vnc_url: "", vm_enabled: false, vnc_ws_port: 6901 };
let _vncLoaded = false;

export function setVncConfig(config) {
  _vncConfig = config;
  _vncLoaded = false; // reset so ensureVncLoaded re-applies on next call

  const btn = document.getElementById("btn-open-vm");
  if (btn && config.vm_enabled && config.vnc_url) {
    btn.href = config.vnc_url;
  }
  // Visibility is owned by updateOpenVmButton (called from selectView) so the
  // button only shows when a VM is actually running, not whenever VM is enabled.
}

// Show "Open VM" only when in view-running AND we have a VM URL to point at.
// Called from state.selectView so the button matches the visible view.
export function updateOpenVmButton(activeView) {
  const btn = document.getElementById("btn-open-vm");
  if (!btn) return;
  const shouldShow = activeView === "view-running"
    && _vncConfig.vm_enabled
    && !!_vncConfig.vnc_url;
  btn.style.display = shouldShow ? "" : "none";
}

export function ensureVncLoaded() {
  if (_vncLoaded) return;
  const iframe = document.getElementById("vnc");
  if (!iframe) return;
  const base = _vncConfig.vnc_url || `http://localhost:${_vncConfig.vnc_ws_port || 6901}`;
  iframe.src = `${base}/vnc.html?autoconnect=true&resize=scale&view_only=true`;
  _vncLoaded = true;
}

// ---------------------------------------------------------------------------
// Credentials preset
// ---------------------------------------------------------------------------

let _credentialsPreset = false;

export async function checkCredentialsPreset() {
  const data = await get("/api/automation/credentials-preset");
  if (!data) return;
  _credentialsPreset = !!data.preset;
  if (!_credentialsPreset) return;

  // Hide required fields — credentials come from server config.
  const mainFields = document.getElementById("extract-fields");
  if (mainFields) mainFields.style.display = "none";

  // Show override inputs in the Advanced section.
  const overrideFields = document.getElementById("extract-override-fields");
  if (overrideFields) overrideFields.style.display = "";
}

// ---------------------------------------------------------------------------
// Start Install
// ---------------------------------------------------------------------------

async function handleStartInstall() {
  const btn = document.getElementById("btn-start-install");
  _setBusy(btn, "Starting…");
  try {
    const { ok, data } = await post("/api/automation/start-install");
    if (!ok) alert(data?.detail || "Failed to start install flow");
  } finally {
    _clearBusy(btn, "Start Install");
  }
}

async function handleStartReinstall() {
  const btn = document.getElementById("btn-start-install-reinstall");
  _setBusy(btn, "Starting…");
  try {
    const { ok, data } = await post("/api/automation/start-install");
    if (!ok) alert(data?.detail || "Failed to start install flow");
  } finally {
    _clearBusy(btn, "Start Reinstall");
  }
}

// ---------------------------------------------------------------------------
// Start Runtime
// ---------------------------------------------------------------------------

async function handleStartRuntime() {
  // Main fields when not preset; override fields when preset but user typed something.
  const email = (
    document.getElementById("input-email")?.value.trim() ||
    document.getElementById("input-email-override")?.value.trim() ||
    ""
  );
  const password = (
    document.getElementById("input-password")?.value ||
    document.getElementById("input-password-override")?.value ||
    ""
  );
  const reuseState = document.getElementById("input-reuse-state")?.checked ?? false;
  const restoreGolden = !reuseState;

  if (!_credentialsPreset && (!email || !password)) {
    alert("Please enter your Apple ID email and password.");
    return;
  }

  const btn = document.getElementById("btn-start-runtime");
  _setBusy(btn, "Starting…");
  try {
    const { ok, data } = await post("/api/automation/start-runtime", {
      apple_email: email,
      apple_password: password,
      restore_golden: restoreGolden,
    });
    if (!ok) alert(data?.detail || "Failed to start extraction flow");
  } finally {
    _clearBusy(btn, "Start Extraction");
  }
}

// ---------------------------------------------------------------------------
// Abort
// ---------------------------------------------------------------------------

async function handleAbort() {
  const btn = document.getElementById("btn-abort");
  _setBusy(btn, "Aborting…");
  try {
    await post("/api/automation/abort");
  } finally {
    _clearBusy(btn, "Abort");
  }
}

// ---------------------------------------------------------------------------
// 2FA
// ---------------------------------------------------------------------------

async function handle2faSubmit() {
  const code = document.getElementById("twofa-code")?.value.trim();
  if (!code) return;
  const btn = document.getElementById("btn-twofa-submit");
  _setBusy(btn, "Verifying…");
  try {
    const { ok, data } = await post("/api/vm/apple-signin/2fa", { code });
    if (!ok) {
      alert(data?.detail || "Failed to submit 2FA code");
    } else {
      const inp = document.getElementById("twofa-code");
      if (inp) inp.value = "";
    }
  } finally {
    _clearBusy(btn, "Verify");
  }
}

async function handleRequestSms() {
  const btn = document.getElementById("btn-request-sms");
  _setBusy(btn, "Requesting…");
  try {
    const { ok, data } = await post("/api/vm/apple-signin/request-sms");
    if (!ok) alert(data?.detail || "Failed to request SMS");
  } finally {
    _clearBusy(btn, "Send SMS code instead");
  }
}

export function setSmsPhone(phone) {
  const el = document.getElementById("twofa-sms-phone");
  if (!el) return;
  if (phone) {
    el.textContent = `SMS code sent to ${phone}`;
    el.style.display = "";
  } else {
    el.style.display = "none";
  }
}

// ---------------------------------------------------------------------------
// Abort button visibility
// ---------------------------------------------------------------------------

export function updateAbortButton(running) {
  const btn = document.getElementById("btn-abort");
  if (btn) btn.style.display = running ? "" : "none";
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _setBusy(btn, text) {
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = text;
}

function _clearBusy(btn, text) {
  if (!btn) return;
  btn.disabled = false;
  btn.textContent = text;
}

// ---------------------------------------------------------------------------
// Wire all buttons
// ---------------------------------------------------------------------------

export function wireButtons() {
  document.getElementById("btn-start-install")?.addEventListener("click", handleStartInstall);
  document.getElementById("btn-start-install-reinstall")?.addEventListener("click", handleStartReinstall);
  document.getElementById("btn-start-runtime")?.addEventListener("click", handleStartRuntime);
  document.getElementById("btn-abort")?.addEventListener("click", handleAbort);
  document.getElementById("btn-twofa-submit")?.addEventListener("click", handle2faSubmit);
  document.getElementById("btn-request-sms")?.addEventListener("click", handleRequestSms);

  document.getElementById("twofa-code")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") handle2faSubmit();
  });

  checkCredentialsPreset();
}
