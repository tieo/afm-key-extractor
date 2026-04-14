// Slide-in panel controller.

const PANELS = ["log", "account", "vm", "settings", "detail"];
const onOpen = {};

export function registerPanelOpen(name, fn) { onOpen[name] = fn; }

export function closeAllPanels() {
  PANELS.forEach((n) => document.getElementById(n + "-panel")?.classList.add("hidden"));
}

export function openPanel(name) {
  closeAllPanels();
  document.getElementById(name + "-panel")?.classList.remove("hidden");
  onOpen[name]?.();
}

export function togglePanel(name) {
  const el = document.getElementById(name + "-panel");
  const wasHidden = el.classList.contains("hidden");
  closeAllPanels();
  if (wasHidden) {
    el.classList.remove("hidden");
    onOpen[name]?.();
  }
}

export function isPanelOpen(name) {
  return !document.getElementById(name + "-panel")?.classList.contains("hidden");
}
