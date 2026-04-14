// App bootstrap: wire buttons, register panels, kick off polling.

import { refreshStatus, onStateChange } from "./state.js";
import { togglePanel, registerPanelOpen, isPanelOpen } from "./panels.js";
import { renderKeysMain, renderCta, handleCta } from "./keys-view.js";
import { renderStatusBar } from "./status-bar.js";
import { renderAccount } from "./account.js";
import { enterVmPanel, renderVmStatus, toggleVncInteract } from "./vm.js";
import { enterSettings, savePollingSettings } from "./settings.js";
import { refreshLog, wireLogFilters, startLogAutoRefresh } from "./log.js";
import { pollNow, syncKeys } from "./actions.js";

// Panel open hooks
registerPanelOpen("log", refreshLog);
registerPanelOpen("account", renderAccount);
registerPanelOpen("vm", enterVmPanel);
registerPanelOpen("settings", enterSettings);

// State-driven re-renders
onStateChange(() => {
  renderStatusBar();
  renderKeysMain();
  renderCta();
  if (isPanelOpen("vm")) renderVmStatus();
  if (isPanelOpen("account")) renderAccount();
});

// Wire data-action attributes declaratively
const actions = {
  "poll-now": (ev) => pollNow(ev.currentTarget),
  "sync-keys": (ev) => syncKeys(ev.currentTarget),
  "open-log": () => togglePanel("log"),
  "open-settings": () => togglePanel("settings"),
  "open-account": () => togglePanel("account"),
  "open-vm": () => togglePanel("vm"),
  "close-log": () => togglePanel("log"),
  "close-account": () => togglePanel("account"),
  "close-vm": () => togglePanel("vm"),
  "close-settings": () => togglePanel("settings"),
  "close-detail": () => togglePanel("detail"),
  "cta": () => handleCta(),
  "toggle-vnc-interact": () => toggleVncInteract(),
  "save-polling": () => savePollingSettings(),
};
document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-action]");
  if (!el) return;
  const fn = actions[el.dataset.action];
  if (fn) { ev.preventDefault(); fn(ev); }
});
document.querySelectorAll('input[data-action="save-polling"]').forEach((i) => {
  i.addEventListener("change", savePollingSettings);
});

wireLogFilters();
startLogAutoRefresh();

refreshStatus();
setInterval(refreshStatus, 10000);
