// Activity log panel.

import { getJSON } from "./api.js";
import { isPanelOpen } from "./panels.js";

let logFilter = "";

export function wireLogFilters() {
  document.querySelectorAll(".log-filters .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      logFilter = chip.dataset.cat || "";
      document.querySelectorAll(".log-filters .chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      refreshLog();
    });
  });
}

export async function refreshLog() {
  const url = logFilter ? `/api/log?cat=${logFilter}&limit=200` : "/api/log?limit=200";
  const entries = await getJSON(url);
  document.getElementById("log-entries").innerHTML = entries.reverse().map((e) => {
    const ts = new Date(e.ts).toLocaleTimeString();
    return `<div class="log-entry ${e.level}">
      <span class="log-ts">${ts}</span><span class="log-cat">${e.cat}</span><span class="log-msg">${e.msg}</span>
    </div>`;
  }).join("");
}

export function startLogAutoRefresh() {
  setInterval(() => { if (isPanelOpen("log")) refreshLog(); }, 5000);
}
