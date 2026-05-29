// First-time setup wizard: download macOS BaseSystem.

import { get, post } from "./api.js";

let _pollTimer = null;

// ---------------------------------------------------------------------------
// Setup status
// ---------------------------------------------------------------------------

/**
 * Fetch /api/setup/status. Returns the status object so callers can cache it
 * for view selection. Also resumes download polling if a download is in progress.
 */
export async function checkSetupStatus() {
  const status = await get("/api/setup/status");
  if (!status) return null;

  // If basesystem is missing and a download is already running, resume polling.
  if (!status.basesystem_ready) {
    const dlStatus = await get("/api/setup/download-macos/status");
    if (dlStatus && dlStatus.running) {
      _startPolling();
    }
  }

  return status;
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
    _setError(data?.detail || "Failed to start download");
    if (btn) { btn.disabled = false; btn.textContent = "Download macOS"; }
    return;
  }
  if (data?.status === "already_present") {
    // Server already has it — trigger a status refresh so the view switches.
    window.dispatchEvent(new CustomEvent("setup-complete"));
    return;
  }
  _startPolling();
}

function _startPolling() {
  if (_pollTimer) return;
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
    _stopPolling();
    window.dispatchEvent(new CustomEvent("setup-complete"));
  }
}

function _stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

function _guessProgress(text) {
  if (!text) return 5;
  const t = text.toLowerCase();
  if (t.includes("converting") || t.includes("qemu-img")) return 80;
  if (t.includes("ready") || t.includes("present")) return 100;
  // fetch-MacOS.py emits "267.0/843.4 MB |...| 31.7% downloaded" — prefer the
  // explicit percentage over the cumulative-byte ratio because the latter
  // misses the 70% scale offset and the conversion phase.
  const pct = text.match(/(\d+(?:\.\d+)?)\s*%/);
  if (pct) return Math.min(Math.round(parseFloat(pct[1]) * 0.7) + 5, 75);
  const m = t.match(/(\d+)\s*\/\s*(\d+)/);
  if (m) return Math.min(Math.round((parseInt(m[1]) / parseInt(m[2])) * 70) + 5, 75);
  return 30;
}

// Strip the upstream ASCII progress bar and reformat to a clean single line.
// Input : "267.0/843.4 MB |======== | 31.7% downloaded"
// Output: "267.0 / 843.4 MB (31.7%)"
function _cleanProgress(text) {
  if (!text) return text;
  const m = text.match(
    /(\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)\s*([KMG]?B)\s*\|[^|]*\|\s*(\d+(?:\.\d+)?)\s*%/i,
  );
  if (m) return `${m[1]} / ${m[2]} ${m[3]} (${m[4]}%)`;
  return text;
}

function _setProgress(text, pct) {
  const textEl = document.getElementById("download-progress-text");
  if (textEl) textEl.textContent = _cleanProgress(text);
  // Drive the pipeline's phase-0 (recovery image) background fill, not a
  // separate in-card bar. One progress visualization, in the navbar.
  import("./state.js").then((mod) => mod.setPipelineProgress?.(0, pct));
}

function _setError(msg) {
  const el = document.getElementById("download-error");
  if (el) { el.textContent = `Download failed: ${msg}`; el.style.display = ""; }
}

// ---------------------------------------------------------------------------
// Wire button
// ---------------------------------------------------------------------------

export function wireSetupButtons() {
  document.getElementById("btn-download-macos")
    ?.addEventListener("click", handleDownloadMacOS);
}
