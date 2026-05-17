// Handles: 2FA form, noVNC iframe, Start Install / Start Runtime, Abort.

import { post, get } from "./api.js";
import { updateUI } from "./state.js";

// ---------------------------------------------------------------------------
// noVNC iframe
// ---------------------------------------------------------------------------

let _vncPort = null;
let _vncLoaded = false;

/**
 * Configure the noVNC iframe src. Called once the VNC port is known.
 */
export function setVncPort(port) {
  _vncPort = port;
}

/**
 * Ensure the noVNC iframe has the correct src.
 */
export function ensureVncLoaded() {
  if (_vncLoaded) return;
  const iframe = document.getElementById("vnc");
  if (!iframe) return;
  const port = _vncPort || window.VNC_WS_PORT || 6901;
  const host = location.hostname;
  iframe.src = `http://${host}:${port}/vnc.html?autoconnect=true&resize=scale&view_only=true`;
  _vncLoaded = true;
}

// ---------------------------------------------------------------------------
// Start Install
// ---------------------------------------------------------------------------

export async function handleStartInstall() {
  const btn = document.getElementById("btn-start-install");
  if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
  try {
    const { ok, data } = await post("/api/automation/start-install");
    if (!ok) {
      alert(data?.detail || "Failed to start install flow");
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Start Install"; }
  }
}

// ---------------------------------------------------------------------------
// Start Runtime
// ---------------------------------------------------------------------------

export async function handleStartRuntime() {
  const email = document.getElementById("input-email")?.value.trim();
  const password = document.getElementById("input-password")?.value;
  const restoreGolden = document.getElementById("input-restore-golden")?.checked ?? true;

  if (!email || !password) {
    alert("Please enter your Apple ID email and password.");
    return;
  }

  const btn = document.getElementById("btn-start-runtime");
  if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
  try {
    const { ok, data } = await post("/api/automation/start-runtime", {
      apple_email: email,
      apple_password: password,
      restore_golden: restoreGolden,
    });
    if (!ok) {
      alert(data?.detail || "Failed to start extraction flow");
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Start Extraction"; }
  }
}

// ---------------------------------------------------------------------------
// Abort
// ---------------------------------------------------------------------------

export async function handleAbort() {
  const btn = document.getElementById("btn-abort");
  if (btn) { btn.disabled = true; btn.textContent = "Aborting…"; }
  try {
    await post("/api/automation/abort");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Abort"; }
  }
}

// ---------------------------------------------------------------------------
// 2FA
// ---------------------------------------------------------------------------

export async function handle2faSubmit() {
  const code = document.getElementById("twofa-code")?.value.trim();
  if (!code) return;
  const btn = document.getElementById("btn-twofa-submit");
  if (btn) { btn.disabled = true; btn.textContent = "Verifying…"; }
  try {
    const { ok, data } = await post("/api/vm/apple-signin/2fa", { code });
    if (!ok) {
      alert(data?.detail || "Failed to submit 2FA code");
    } else {
      // Clear the input so it's ready if needed again.
      const inp = document.getElementById("twofa-code");
      if (inp) inp.value = "";
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Verify Code"; }
  }
}

export async function handleRequestSms() {
  const btn = document.getElementById("btn-request-sms");
  if (btn) { btn.disabled = true; btn.textContent = "Requesting…"; }
  try {
    const { ok, data } = await post("/api/vm/apple-signin/request-sms");
    if (!ok) {
      alert(data?.detail || "Failed to request SMS");
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Send SMS code instead"; }
  }
}

/**
 * Update the SMS phone hint inside the 2FA form.
 */
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
// Wire all buttons
// ---------------------------------------------------------------------------

export function wireButtons() {
  document.getElementById("btn-start-install")?.addEventListener("click", handleStartInstall);
  document.getElementById("btn-start-runtime")?.addEventListener("click", handleStartRuntime);
  document.getElementById("btn-abort")?.addEventListener("click", handleAbort);
  document.getElementById("btn-twofa-submit")?.addEventListener("click", handle2faSubmit);
  document.getElementById("btn-request-sms")?.addEventListener("click", handleRequestSms);

  // Allow Enter in 2FA input to submit.
  document.getElementById("twofa-code")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") handle2faSubmit();
  });
}
