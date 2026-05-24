// First-time setup wizard: download macOS BaseSystem, then unlock install card.

import { get, post } from "./api.js";

let _pollTimer = null;

// ---------------------------------------------------------------------------
// Setup status check
// ---------------------------------------------------------------------------

/**
 * Fetch /api/setup/status and update UI visibility.
 * - If basesystem_ready: hide card-setup, show card-install (and card-runtime
 *   if golden_image_ready).
 * - Otherwise: show card-setup, hide card-install, hide card-runtime.
 * Called on page load and after download completes.
 */
export async function checkSetupStatus() {
  const status = await get("/api/setup/status");
  if (!status) return; // server not ready yet

  const cardSetup = document.getElementById("card-setup");
  const cardInstall = document.getElementById("card-install");
  const cardRuntime = document.getElementById("card-runtime");

  if (status.basesystem_ready) {
    if (cardSetup) cardSetup.style.display = "none";
    if (cardInstall) cardInstall.style.display = "";
    if (cardRuntime) cardRuntime.style.display = status.golden_image_ready ? "" : "none";
  } else {
    if (cardSetup) cardSetup.style.display = "";
    if (cardInstall) cardInstall.style.display = "none";
    if (cardRuntime) cardRuntime.style.display = "none";
    // If a download is already running (server restarted mid-download), resume polling.
    const dlStatus = await get("/api/setup/download-macos/status");
    if (dlStatus && dlStatus.running) {
      _startPolling();
    }
  }
}

// ---------------------------------------------------------------------------
// Download flow
// ---------------------------------------------------------------------------

export async function handleDownloadMacOS() {
  const btn = document.getElementById("btn-download-macos");
  if (btn) { btn.disabled = true; btn.textContent = "Downloading…"; }

  const progressEl = document.getElementById("download-progress");
  const errorEl = document.getElementById("download-error");
  if (progressEl) progressEl.style.display = "";
  if (errorEl) errorEl.style.display = "none";
  _setProgress("Starting download…", 5);

  const { ok, data } = await post("/api/setup/download-macos");
  if (!ok) {
    const msg = data?.detail || "Failed to start download";
    _setError(msg);
    if (btn) { btn.disabled = false; btn.textContent = "Download macOS"; }
    return;
  }
  if (data?.status === "already_present") {
    // Race: another client already downloaded it between our status check and click.
    await checkSetupStatus();
    return;
  }
  _startPolling();
}

function _startPolling() {
  if (_pollTimer) return; // already polling
  _pollTimer = setInterval(_pollDownload, 2000);
}

async function _pollDownload() {
  const status = await get("/api/setup/download-macos/status");
  if (!status) return;

  if (status.error) {
    _stopPolling();
    _setError(status.error);
    const btn = document.getElementById("btn-download-macos");
    if (btn) { btn.disabled = false; btn.textContent = "Retry Download"; }
    return;
  }

  _setProgress(status.progress || "Downloading…", _guessProgress(status.progress));

  if (!status.running && !status.error) {
    // Download finished — re-check setup status which will unhide the install card.
    _stopPolling();
    await checkSetupStatus();
  }
}

function _stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

/**
 * Guess a 0–100 fill percentage from the progress string so the bar moves.
 * The download goes fetch → convert, so we split the range 5–95.
 */
function _guessProgress(text) {
  if (!text) return 5;
  const t = text.toLowerCase();
  if (t.includes("converting") || t.includes("qemu-img")) return 80;
  if (t.includes("ready") || t.includes("present")) return 100;
  // fetch-macOS.py prints "Downloading ... chunk N/M" style output.
  const m = t.match(/(\d+)\s*\/\s*(\d+)/);
  if (m) {
    const pct = Math.round((parseInt(m[1]) / parseInt(m[2])) * 70) + 5;
    return Math.min(pct, 75);
  }
  return 30;
}

function _setProgress(text, pct) {
  const textEl = document.getElementById("download-progress-text");
  const fill = document.getElementById("download-progress-fill");
  if (textEl) textEl.textContent = text;
  if (fill) fill.style.width = `${pct}%`;
}

function _setError(msg) {
  const el = document.getElementById("download-error");
  if (el) { el.textContent = `Download failed: ${msg}`; el.style.display = ""; }
  const fill = document.getElementById("download-progress-fill");
  if (fill) fill.style.background = "#e74c3c";
}

// ---------------------------------------------------------------------------
// Wire button
// ---------------------------------------------------------------------------

export function wireSetupButtons() {
  document.getElementById("btn-download-macos")
    ?.addEventListener("click", handleDownloadMacOS);
}
