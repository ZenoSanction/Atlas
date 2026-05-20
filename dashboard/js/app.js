// ATLAS dashboard — main script.
// Single-page app, no build step required. Pure ES modules in the browser.

import { initTabs } from "/static/js/tabs.js";
import { connectEvents } from "/static/js/ws.js";
import { renderMissionControl, refreshMissionControl } from "/static/js/mission-control.js";
import { renderWeather, refreshWeather } from "/static/js/weather.js";
import { renderPlan } from "/static/js/plan.js";
import { renderScience } from "/static/js/science.js";
import { renderHistory } from "/static/js/history.js";
import { initChat } from "/static/js/atlas-chat.js";
import { initSetup } from "/static/js/setup.js";

const api = (path, opts = {}) =>
  fetch(`/api${path}`, {
    headers: { "content-type": "application/json", ...(opts.headers || {}) },
    ...opts,
  }).then(async (r) => {
    if (!r.ok) {
      let err = `HTTP ${r.status}`;
      try { const j = await r.json(); err = j.detail || err; } catch {}
      throw new Error(err);
    }
    return r.json();
  });

window.atlas = { api };

// Boot
(async function boot() {
  initTabs({
    tonight: refreshMissionControl,
    weather: renderWeather,
    plan: renderPlan,
    science: renderScience,
    history: renderHistory,
    atlas: () => {},
    setup: () => initSetup(api),
  });
  initChat(api);
  connectEvents();

  // version pill
  try {
    const h = await api("/health");
    document.getElementById("version-pill").textContent = h.version;
  } catch (e) {
    document.getElementById("version-pill").textContent = "offline";
  }

  // Mission Control is the new Tonight view. It self-polls every 3 s
  // inside its module (cheap in-memory state read), so we just kick it
  // off once at boot.
  renderMissionControl(api);

  // Take Control toggle
  const tc = document.getElementById("take-control");
  tc.addEventListener("click", async () => {
    tc.classList.toggle("active");
    const taking = tc.classList.contains("active");
    tc.textContent = taking ? "Release Control" : "Take Control";
    try {
      await api("/tonight/command", {
        method: "POST",
        body: JSON.stringify({
          command: taking ? "take_control" : "release_control",
          params: {},
        }),
      });
    } catch (e) {
      console.error("Take control failed:", e);
    }
  });
})();
