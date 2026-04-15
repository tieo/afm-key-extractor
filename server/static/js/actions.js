// Global actions wired to the toolbar.

import { postJSON, toast, busy } from "./api.js";
import { refreshStatus } from "./state.js";

export async function pollNow(btn) {
  await busy(btn, "Refreshing…", async () => {
    const { ok, data } = await postJSON("/api/poll");
    if (!ok || data.error) toast(data.error || "Refresh failed", "error");
    else toast("Locations refreshed");
    await refreshStatus();
  });
}

export async function syncKeys(btn) {
  await busy(btn, "Syncing…", async () => {
    const { ok, data } = await postJSON("/api/extract-keys");
    if (!ok || data.error) toast(data.error || "Sync failed", "error");
    else if (data.status === "already_running") toast("Sync already in progress");
    else toast("Sync started — keys will appear when the VM finishes");
    await refreshStatus();
  });
}
