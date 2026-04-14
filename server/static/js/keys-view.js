// Main screen keys list, CTA banner, per-key drill-down with map.

import { timeAgo, getJSON } from "./api.js";
import { state } from "./state.js";
import { openPanel, togglePanel } from "./panels.js";
import { syncKeys } from "./actions.js";

let detailMap = null, detailMarker = null, detailLine = null;
let tagIcon = null;

function ensureDetailMap() {
  if (detailMap) return detailMap;
  tagIcon = L.divIcon({
    html: '<div style="background:#4cc9f0;width:14px;height:14px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 6px rgba(0,0,0,.4)"></div>',
    iconSize: [14, 14], className: "",
  });
  detailMap = L.map("detail-map").setView([51.1, 10.4], 6);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OSM", maxZoom: 19,
  }).addTo(detailMap);
  return detailMap;
}

export function renderKeysMain() {
  const el = document.getElementById("keys-main");
  if (!state.keys.length) {
    el.innerHTML = `<div class="empty-state">No AirTag keys yet.<br>Upload a .json in Settings, or Sync from the VM.</div>`;
    return;
  }
  const tagById = {};
  state.tags.forEach((t) => (tagById[t.airtag_id] = t));
  el.innerHTML = state.keys.map((k) => {
    const id = k.file ? k.file.replace(".json", "") : k.name;
    const t = tagById[id] || tagById[k.name];
    const locLabel = t
      ? `${timeAgo(t.timestamp)} · ${t.latitude.toFixed(3)}, ${t.longitude.toFixed(3)}`
      : "no recent location";
    const dot = t ? "green" : "gray";
    return `<div class="key-card" data-id="${encodeURIComponent(id)}">
      <div class="key-icon">📍</div>
      <div class="key-body">
        <div class="key-name">${k.name}</div>
        <div class="key-meta">${locLabel}</div>
      </div>
      <div class="key-status"><span class="dot ${dot}"></span></div>
    </div>`;
  }).join("");
  el.querySelectorAll(".key-card").forEach((card) => {
    card.addEventListener("click", () => openKeyDetail(decodeURIComponent(card.dataset.id)));
  });
}

export function renderCta() {
  const banner = document.getElementById("cta-banner");
  const text = document.getElementById("cta-text");
  let msg = null, action = null;
  if (!state.account.configured) {
    msg = "Sign in to Apple ID to start tracking";
    action = () => togglePanel("account");
  } else if (state.vm.enabled && !state.vm.setup_complete) {
    msg = "Finish macOS VM setup to extract AirTag keys";
    action = () => togglePanel("vm");
  } else if (state.keys.length === 0) {
    msg = state.vm.enabled ? "Sync keys from the VM keychain" : "Upload AirTag .json keys";
    action = state.vm.enabled ? () => syncKeys() : () => togglePanel("settings");
  }
  if (msg) {
    text.textContent = msg;
    banner.style.display = "";
    banner._action = action;
  } else {
    banner.style.display = "none";
  }
}

export function handleCta() {
  const b = document.getElementById("cta-banner");
  if (b._action) b._action();
}

export async function openKeyDetail(id) {
  const key = state.keys.find((k) => (k.file || "").replace(".json", "") === id || k.name === id);
  if (!key) return;
  document.getElementById("detail-title").textContent = key.name;
  openPanel("detail");
  const t = state.tags.find((x) => x.airtag_id === id);
  document.getElementById("detail-meta").innerHTML = t
    ? `Last seen <b>${timeAgo(t.timestamp)}</b><br>${t.latitude.toFixed(5)}, ${t.longitude.toFixed(5)}`
    : "No recent location reports.";
  setTimeout(async () => {
    const m = ensureDetailMap();
    m.invalidateSize();
    if (detailMarker) { m.removeLayer(detailMarker); detailMarker = null; }
    if (detailLine) { m.removeLayer(detailLine); detailLine = null; }
    if (!t) return;
    detailMarker = L.marker([t.latitude, t.longitude], { icon: tagIcon }).addTo(m);
    m.setView([t.latitude, t.longitude], 14);
    const pts = await getJSON(`/api/airtags/${id}/history?limit=500`);
    if (!pts.length) return;
    const latlngs = pts.map((p) => [p.latitude, p.longitude]).reverse();
    detailLine = L.polyline(latlngs, { color: "#4cc9f0", weight: 3, opacity: 0.7 }).addTo(m);
    m.fitBounds(detailLine.getBounds(), { padding: [30, 30] });
  }, 50);
}
