// macOS VM panel — VNC iframe + controls.

import { postJSON, toast, busy } from "./api.js";
import { state, refreshStatus } from "./state.js";

const VNC_URL = `//${location.hostname.replace("airtag", "airtag-vnc")}/vnc.html?autoconnect=true&resize=scale&view_only=true`;
let vncLoaded = false;
let vmStartTime = null;
let vmOverlayTimer = null;

export function enterVmPanel() {
  if (!vncLoaded) {
    document.getElementById("vnc-iframe").src = VNC_URL;
    vncLoaded = true;
  }
  renderVmStatus();
}

export function renderVmStatus() {
  const summary = document.getElementById("vm-status-summary");
  const controls = document.getElementById("vm-controls");

  if (!state.vm.enabled) {
    summary.innerHTML = '<span class="dot gray"></span> VM not enabled on this server.';
    controls.innerHTML = "";
    return;
  }

  let label, cls;
  if (!state.vm.provisioned) { label = "Downloading macOS…"; cls = "yellow"; }
  else if (!state.vm.setup_complete) { label = "Needs initial setup"; cls = "yellow"; }
  else if (state.vm.vm_running) { label = "Running"; cls = "green"; }
  else { label = "Ready"; cls = "green"; }
  summary.innerHTML = `<span class="dot ${cls}"></span> ${label}`;

  if (state.vm.vm_running && vmOverlayTimer) hideVmOverlay();

  controls.innerHTML = "";
  if (!state.vm.vm_running) {
    controls.append(button("Start VM (auto-boot)", "primary", (ev) => vmStartSetup(ev.currentTarget)));
    controls.append(button("Start (manual)", "", (ev) => vmAction("/api/vm/start-manual", "Starting VM", ev.currentTarget)));
    controls.append(button("Reset to golden", "danger", (ev) => {
      if (!confirm("Overwrite the VM disk with the golden snapshot? All session state since the last bake will be lost.")) return;
      vmAction("/api/vm/reset-to-golden", "Resetting to golden", ev.currentTarget);
    }));
  } else {
    controls.append(button("Stop VM", "", (ev) => vmAction("/api/vm/stop", "Stopping VM", ev.currentTarget)));
  }
  controls.append(button("Bake golden image", "danger", (ev) => {
    if (!confirm("Bake the current VM disk as the golden image? The VM must be fully set up.")) return;
    vmAction("/api/vm/bake-golden", "Baking golden", ev.currentTarget);
  }));
}

function button(text, extraClass, onClick) {
  const b = document.createElement("button");
  b.className = "btn " + extraClass;
  b.textContent = text;
  b.addEventListener("click", onClick);
  return b;
}

async function vmAction(url, label, btn) {
  return busy(btn, label + "…", async () => {
    const { ok, data } = await postJSON(url);
    if (!ok || data.error) { toast(data.error || label + " failed", "error"); return false; }
    toast(label + "…");
    await refreshStatus();
    return true;
  });
}

async function vmStartSetup(btn) {
  showVmOverlay("Starting VM — booting macOS…");
  enterVmPanel();
  const ok = await vmAction("/api/vm/start-setup", "Starting VM", btn);
  if (!ok) { hideVmOverlay(); return; }
  setTimeout(() => {
    const f = document.getElementById("vnc-iframe");
    if (f.src) f.src = f.src;
  }, 3000);
}

function showVmOverlay(label) {
  document.getElementById("vnc-overlay-label").textContent = label;
  document.getElementById("vnc-overlay").style.display = "flex";
  vmStartTime = Date.now();
  clearInterval(vmOverlayTimer);
  vmOverlayTimer = setInterval(() => {
    const s = Math.round((Date.now() - vmStartTime) / 1000);
    document.getElementById("vnc-overlay-elapsed").textContent = `${s}s elapsed`;
  }, 1000);
}

function hideVmOverlay() {
  document.getElementById("vnc-overlay").style.display = "none";
  clearInterval(vmOverlayTimer);
  vmOverlayTimer = null;
}

export function toggleVncInteract() {
  const f = document.getElementById("vnc-iframe"), b = document.getElementById("vnc-interact");
  if (f.src.includes("view_only=true")) {
    f.src = f.src.replace("view_only=true", "view_only=false");
    b.textContent = "Interactive"; b.style.color = "#4cc9f0";
  } else {
    f.src = f.src.replace("view_only=false", "view_only=true");
    b.textContent = "View Only"; b.style.color = "#aaa";
  }
}
