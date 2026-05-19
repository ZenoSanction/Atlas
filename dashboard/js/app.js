// ATLAS dashboard — main script.
// Single-page app, no build step required. Pure ES modules in the browser.

import { initTabs } from "/static/js/tabs.js";
import { connectEvents } from "/static/js/ws.js";
import { renderTonight, refreshTonight } from "/static/js/tonight.js";
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
    tonight: refreshTonight,
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

  // Live state pushes come via WebSocket; this poll is the belt-and-braces
  // fallback. 15 s avoids saturating the browser's 6-concurrent-fetches
  // limit if the underlying /api/tonight/status is ever slow (e.g. NINA
  // not responding).
  renderTonight(api);
  setInterval(() => refreshTonight(api), 15000);

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
