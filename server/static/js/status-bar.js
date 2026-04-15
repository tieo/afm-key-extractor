// Toolbar/status-bar rendering (runs on every state refresh).

import { state } from "./state.js";

export function renderStatusBar() {
  const pollState = state.settings.state || {};
  const mode = pollState.moving ? "active" : "idle";
  const interval = Math.round((pollState.current_interval || state.settings.idle_interval) / 60);
  document.getElementById("tags-status").innerHTML =
    `${state.keys.length} key${state.keys.length !== 1 ? "s" : ""} · ${mode} · every ${interval}min`;

  const accBadge = document.getElementById("badge-account");
  accBadge.firstElementChild.className = "dot " + (state.account.configured ? "green" : "red");
  accBadge.lastElementChild.textContent = "Apple ID: " + (state.account.configured ? "connected" : "sign in");

  const vmBadge = document.getElementById("badge-vm");
  if (!state.vm.enabled) { vmBadge.style.display = "none"; return; }
  vmBadge.style.display = "";
  let label = "VM ", cls = "gray";
  if (!state.vm.provisioned) { label += "downloading"; cls = "yellow"; }
  else if (!state.vm.setup_complete) { label += "needs setup"; cls = "yellow"; }
  else if (state.vm.vm_running) { label += "running"; cls = "green"; }
  else { label += "ready"; cls = "green"; }

  const signin = state.vm.apple_signin || {};
  let signinLabel = {
    running: { text: "signing in…", cls: "yellow" },
    awaiting_2fa: { text: "needs 2FA", cls: "yellow" },
    signed_in: { text: "iCloud ✓", cls: "green" },
    failed: { text: "sign-in failed", cls: "red" },
  }[signin.state];
  if (!signinLabel) {
    signinLabel = signin.signed_in_cached
      ? { text: "iCloud ✓", cls: "green" }
      : { text: "iCloud: not signed in", cls: "yellow" };
  }
  label += " · " + signinLabel.text;
  if (signinLabel.cls === "red" || signinLabel.cls === "yellow") cls = signinLabel.cls;
  vmBadge.firstElementChild.className = "dot " + cls;
  vmBadge.lastElementChild.textContent = label;
  vmBadge.title = signin.error || "";
}
