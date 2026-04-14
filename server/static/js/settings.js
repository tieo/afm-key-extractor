// Settings panel: polling config + keys list + drop-zone upload.

import { getJSON, putJSON, del, timeAgo } from "./api.js";
import { refreshStatus } from "./state.js";

export async function enterSettings() {
  await loadPollingSettings();
  await loadKeysList();
  wireDropZone();
}

async function loadPollingSettings() {
  const data = await getJSON("/api/settings");
  document.getElementById("set-adaptive").checked = data.adaptive !== false;
  document.getElementById("set-idle").value = Math.round(data.idle_interval / 60);
  document.getElementById("set-active").value = Math.round(data.active_interval / 60);
  document.getElementById("set-threshold").value = data.movement_threshold;
  document.getElementById("set-cooldown").value = data.cooldown_polls;
  const s = data.state || {};
  const mode = s.moving ? "Active" : "Idle";
  const iv = Math.round((s.current_interval || data.idle_interval) / 60);
  const lp = s.last_poll ? timeAgo(s.last_poll) : "never";
  document.getElementById("poll-status").innerHTML =
    `<span class="dot ${s.moving ? "green" : "yellow"}"></span> ${mode} · every ${iv}min · last: ${lp}`;
}

export async function savePollingSettings() {
  await putJSON("/api/settings", {
    adaptive: document.getElementById("set-adaptive").checked,
    idle_interval: parseInt(document.getElementById("set-idle").value) * 60,
    active_interval: parseInt(document.getElementById("set-active").value) * 60,
    movement_threshold: parseInt(document.getElementById("set-threshold").value),
    cooldown_polls: parseInt(document.getElementById("set-cooldown").value),
  });
  await loadPollingSettings();
}

async function loadKeysList() {
  const keys = await getJSON("/api/keys");
  const el = document.getElementById("keys-list");
  if (!keys.length) { el.innerHTML = '<div style="color:#666">No keys loaded.</div>'; return; }
  el.innerHTML = keys.map((k) => `
    <div class="key-item">
      <div><div>${k.name}</div><div class="key-meta">${k.model || ""}</div></div>
      <button class="btn small danger" data-file="${k.file.replace(".json", "")}">Remove</button>
    </div>`).join("");
  el.querySelectorAll("button[data-file]").forEach((b) => {
    b.addEventListener("click", () => deleteKey(b.dataset.file));
  });
}

async function deleteKey(name) {
  if (!confirm(`Remove "${name}"?`)) return;
  await del(`/api/keys/${name}`);
  await loadKeysList();
  await refreshStatus();
}

let dropZoneWired = false;
function wireDropZone() {
  if (dropZoneWired) return;
  dropZoneWired = true;
  const dropZone = document.getElementById("drop-zone");
  const fileInput = document.getElementById("key-file-input");
  dropZone.addEventListener("click", () => fileInput.click());
  dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("dragover"); });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    handleFiles(e.dataTransfer.files);
  });
  fileInput.addEventListener("change", () => handleFiles(fileInput.files));
}

async function handleFiles(files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch("/api/keys/upload", { method: "POST", body: fd });
    const d = await r.json().catch(() => ({}));
    if (d.error) alert("Failed: " + d.error);
  }
  await loadKeysList();
  await refreshStatus();
}
