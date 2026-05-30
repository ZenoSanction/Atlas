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
import { initManualControl, refreshManualControl } from "/static/js/manual-control.js";
import { initRightNow, refreshPlanHistory } from "/static/js/right-now.js";

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
    // tabs.js calls handlers with window.atlas.api; forward it so
    // renderPlan can fetch /plan/tonight. refreshPlanHistory reads
    // window.atlas.api directly so it doesn't need the argument.
    plan: (api) => { renderPlan(api); refreshPlanHistory(); },
    science: renderScience,
    history: renderHistory,
    atlas: () => {},
    setup: () => initSetup(api),
  });
  initChat(api);
  connectEvents();

  // TTS toggle persists in localStorage. mission-control.js's speakReply
  // reads the same key.
  const tts = document.getElementById("tts-toggle");
  if (tts) {
    tts.checked = localStorage.getItem("atlas_tts_enabled") === "1";
    tts.addEventListener("change", () => {
      localStorage.setItem("atlas_tts_enabled", tts.checked ? "1" : "0");
    });
  }

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

  // Manual control: banner, take/release dialogs, Hardware Controls panel.
  // initManualControl wires the topbar Take Control button, the banner's
  // Release button, every hw-form / hw-quick button on the Tonight tab,
  // and starts a 4s poll against /api/control/status so the audit list
  // stays current.
  initManualControl(api);

  // Right Now bar + Pending Decisions panel — polls /api/right-now
  // every 5 s. Visible across every tab per doctrine.
  initRightNow();
})();
