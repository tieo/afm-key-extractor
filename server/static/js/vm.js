// macOS VM panel — VNC iframe + controls.

import { postJSON, getJSON, toast, busy } from "./api.js";
import { state, refreshStatus } from "./state.js";
import { openPanel } from "./panels.js";
import { showLoginForm } from "./account.js";

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
  else { label = "Stopped"; cls = "gray"; }
  const signin = state.vm.apple_signin || {};
  const signinText = {
    running: "iCloud sign-in in progress…",
    awaiting_2fa: "iCloud sign-in: waiting for 2FA code",
    signed_in: "iCloud ✓",
    failed: "iCloud sign-in failed" + (signin.error ? ` — ${signin.error}` : ""),
  }[signin.state];
  const signinLine = signinText
    ? `<div class="vm-signin-line ${signin.state}">${signinText}</div>`
    : (signin.signed_in_cached ? '<div class="vm-signin-line signed_in">iCloud ✓</div>' : "");
  summary.innerHTML = `<span class="dot ${cls}"></span> ${label}${signinLine}`;

  if (state.vm.vm_running && vmOverlayTimer) hideVmOverlay();

  controls.innerHTML = "";
  const primary = document.createElement("div");
  primary.className = "vm-actions-primary";
  const advanced = document.createElement("details");
  advanced.className = "vm-actions-advanced";
  advanced.innerHTML = '<summary>Advanced</summary>';
  const adv = document.createElement("div");
  adv.className = "vm-actions-advanced-body";
  advanced.append(adv);

  if (!state.vm.vm_running) {
    primary.append(action(
      "Start VM", "primary",
      "Boot macOS and auto-login. Once the VM is up, use “Sign in to iCloud” to drive the Apple ID sign-in.",
      (ev) => vmStartSetup(ev.currentTarget),
    ));
    adv.append(action(
      "Start without automation", "",
      "Boot the VM with no key-typing. For debugging or driving Setup Assistant manually via the VNC view.",
      (ev) => vmAction("/api/vm/start-manual", "Starting VM (manual)", ev.currentTarget),
    ));
    adv.append(action(
      "Reset to golden snapshot", "danger",
      "Overwrite the VM disk with the last baked golden image. Destructive — everything since the last bake is lost.",
      (ev) => {
        if (!confirm("Overwrite the VM disk with the golden snapshot? All session state since the last bake will be lost.")) return;
        vmAction("/api/vm/reset-to-golden", "Resetting to golden", ev.currentTarget);
      },
    ));
  } else {
    primary.append(action(
      "Sign in to iCloud", "primary",
      "Drive Apple ID sign-in inside the running VM. Required once before extracting AirTag keys.",
      (ev) => startAppleSignin(ev.currentTarget),
    ));
    primary.append(action(
      "Stop VM", "",
      "Shut down the VM cleanly.",
      (ev) => vmAction("/api/vm/stop", "Stopping VM", ev.currentTarget),
    ));
  }
  adv.append(action(
    "Bake golden snapshot", "danger",
    "Save the current VM disk as the new golden image. Do this once, after macOS is set up and signed into iCloud, so future resets restore to this clean state.",
    (ev) => {
      if (!confirm("Bake the current VM disk as the golden image? The VM must be fully set up.")) return;
      vmAction("/api/vm/bake-golden", "Baking golden", ev.currentTarget);
    },
  ));

  controls.append(primary, advanced);
}

function action(text, extraClass, description, onClick) {
  const row = document.createElement("div");
  row.className = "vm-action";
  const b = button(text, extraClass, onClick);
  const desc = document.createElement("div");
  desc.className = "vm-action-desc";
  desc.textContent = description;
  row.append(b, desc);
  return row;
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

let signinPoll = null;

async function startAppleSignin(btn) {
  return busy(btn, "Starting…", async () => {
    let res = await postJSON("/api/vm/apple-signin/start");
    if (!res.ok && res.data?.error === "needs_password") {
      // Route through the regular Apple ID login form — it already
      // handles 2FA and, on success, triggers the VM sign-in itself.
      toast("Sign in with Apple ID — the VM sign-in runs automatically");
      openPanel("account");
      showLoginForm();
      return false;
    }
    const { ok, data } = res;
    if (!ok || data.error) { toast(data.error || "Failed to start", "error"); return false; }
    toast("Apple ID sign-in started");
    pollSignin();
    return true;
  });
}

async function pollSignin() {
  if (signinPoll) clearInterval(signinPoll);
  const tick = async () => {
    const { ok, data } = await getJSON("/api/vm/apple-signin/status");
    if (!ok) return;
    const summary = document.getElementById("vm-status-summary");
    if (summary) summary.innerHTML = `<span class="dot yellow"></span> Apple sign-in: ${data.state}${data.error ? " — " + data.error : ""}`;
    if (data.state === "awaiting_2fa") {
      render2faForm(data.sms_phone);
    } else if (data.state === "signed_in" || data.state === "failed" || data.state === "idle") {
      clearInterval(signinPoll); signinPoll = null;
      const f = document.getElementById("vm-2fa-form"); if (f) f.remove();
      await refreshStatus();
    }
  };
  await tick();
  signinPoll = setInterval(tick, 3000);
}

function render2faForm(smsPhone) {
  let form = document.getElementById("vm-2fa-form");
  const controls = document.getElementById("vm-controls");
  if (!controls) return;
  if (!form) {
    form = document.createElement("div");
    form.id = "vm-2fa-form";
    form.className = "section";
    form.innerHTML = `
      <h4>Apple 2FA</h4>
      <p class="vm-2fa-hint">Enter the 6-digit code Apple sent to your trusted device, or request an SMS code.</p>
      <input type="text" id="vm-2fa-code" placeholder="6-digit code" maxlength="6" inputmode="numeric" autocomplete="one-time-code">
      <button class="btn primary" data-action="vm-submit-2fa">Verify</button>
      <button class="btn" data-action="vm-request-sms">Send SMS code</button>
      <div class="vm-2fa-phone" id="vm-2fa-phone"></div>`;
    controls.prepend(form);
    form.querySelector('[data-action="vm-submit-2fa"]').addEventListener("click", submitVm2fa);
    form.querySelector('[data-action="vm-request-sms"]').addEventListener("click", requestVmSms);
    document.getElementById("vm-2fa-code").focus();
  }
  const phoneDiv = document.getElementById("vm-2fa-phone");
  if (phoneDiv) phoneDiv.textContent = smsPhone ? `SMS sent to ${smsPhone}` : "";
}

async function submitVm2fa() {
  const code = document.getElementById("vm-2fa-code").value.trim();
  if (!code) return;
  const { ok, data } = await postJSON("/api/vm/apple-signin/2fa", { code });
  if (!ok || data.error) { toast(data.error || "2FA failed", "error"); return; }
  toast("2FA submitted");
}

async function requestVmSms(ev) {
  const btn = ev.currentTarget;
  return busy(btn, "Requesting…", async () => {
    const { ok, data } = await postJSON("/api/vm/apple-signin/request-sms");
    if (!ok || data.error) { toast(data.error || "Request failed", "error"); return false; }
    toast("SMS requested — check your phone");
    return true;
  });
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
