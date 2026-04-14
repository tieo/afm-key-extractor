// Shared app state + status refresh.

import { getJSON } from "./api.js";

export const state = {
  account: { configured: false, airtags: 0 },
  vm: { enabled: false },
  settings: {},
  tags: [],
  keys: [],
};

const listeners = new Set();
export function onStateChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }

export async function refreshStatus() {
  const [account, vm, settings, tags, keys] = await Promise.all([
    getJSON("/api/account/status"),
    getJSON("/api/vm/status"),
    getJSON("/api/settings"),
    getJSON("/api/airtags"),
    getJSON("/api/keys"),
  ]);
  state.account = account;
  state.vm = vm;
  state.settings = settings;
  state.tags = tags;
  state.keys = keys;
  listeners.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
}
