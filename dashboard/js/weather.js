// Weather tab.
//
// Pulls four pieces of data on render:
//   /api/weather/current      — live snapshot
//   /api/weather/forecast?h=12 — next 12 hours (the "tonight" window)
//   /api/critic/assessment    — per-metric pass/fail from the Critic agent
//   /api/operator/verdict     — final GO / CAUTION / NO-GO from the Operator
//
// Refreshes hourly on its own timer; the Refresh button forces an immediate
// pull. The Critic also pushes new assessments over the WebSocket, but we
// keep the pull-based refresh as a belt-and-braces so the tab is correct
// when first opened.

let _timer = null;

export function renderWeather(api) {
  refreshWeather(api);
  if (_timer === null) {
    // Refresh every 60 minutes
    _timer = setInterval(() => refreshWeather(api), 60 * 60 * 1000);
  }
  const btn = document.getElementById("weather-refresh");
  if (btn && !btn.dataset.bound) {
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => refreshWeather(api));
  }
}

export async function refreshWeather(api) {
  const updatedEl = document.getElementById("weather-updated");
  if (updatedEl) updatedEl.textContent = "refreshing…";
  await Promise.allSettled([
    pullCurrent(api), pullForecast(api),
    pullAssessment(api), pullVerdict(api),
  ]);
  if (updatedEl) {
    const now = new Date();
    updatedEl.textContent = `updated ${now.toLocaleTimeString()}`;
  }
}

async function pullCurrent(api) {
  const el = document.getElementById("weather-now");
  if (!el) return;
  try {
    const c = await api("/weather/current");
    el.innerHTML = `
      <div><span>Site</span><span>${esc(c.observatory_name)}</span></div>
      <div><span>Observed</span><span>${fmtEastern(c.observed_at)}</span></div>
      <div><span>Temperature</span><span>${c.temperature_f} °F</span></div>
      <div><span>Humidity</span><span>${c.humidity_pct} %</span></div>
      <div><span>Dew point</span><span>${c.dew_point_f} °F</span></div>
      <div><span>Dew margin</span><span>${marginLabel(c.dew_margin_f)}</span></div>
      <div><span>Wind</span><span>${c.wind_speed_mph} mph${c.wind_gust_mph != null ? ` (gust ${c.wind_gust_mph})` : ""}</span></div>
      <div><span>Cloud cover</span><span>${c.cloud_cover_pct} %</span></div>
      <div><span>Precip (last hr)</span><span>${c.precip_in} in</span></div>
      <div><span>Pressure</span><span>${c.pressure_inhg} inHg</span></div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

async function pullForecast(api) {
  const el = document.getElementById("weather-forecast");
  if (!el) return;
  try {
    const f = await api("/weather/forecast?hours=24&nighttime_only=true");
    if (!f.hourly || f.hourly.length === 0) {
      el.innerHTML = `<div class="empty">No usable night hours in the next 24 hours (sun-up all the way through).</div>`;
      return;
    }
    let nightHeader = "";
    if (f.night) {
      nightHeader = `<div class="hint">Dark window: ${fmtEastern(f.night.dusk_utc)} → ${fmtEastern(f.night.dawn_utc)} (${f.night.hours} h)</div>`;
    }
    const head = `
      <table class="tbl">
        <thead><tr>
          <th>Time</th>
          <th>Temp</th>
          <th>Humidity</th>
          <th>Dew margin</th>
          <th>Wind</th>
          <th>Gust</th>
          <th>Cloud</th>
          <th>Precip</th>
        </tr></thead><tbody>`;
    const rows = f.hourly.map((h) => {
      const cls = severityFromRow(h);
      return `<tr class="row-${cls}">
        <td>${fmtEastern(h.time_utc)}</td>
        <td>${h.temperature_f} °F</td>
        <td>${h.humidity_pct}%</td>
        <td>${marginLabel(h.dew_margin_f)}</td>
        <td>${h.wind_speed_mph} mph</td>
        <td>${h.wind_gust_mph != null ? h.wind_gust_mph + " mph" : "—"}</td>
        <td>${h.cloud_cover_pct}%</td>
        <td>${h.precip_in} in</td>
      </tr>`;
    }).join("");
    el.innerHTML = nightHeader + head + rows + "</tbody></table>";
  } catch (e) {
    el.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

async function pullAssessment(api) {
  const el = document.getElementById("weather-checks");
  if (!el) return;
  try {
    const r = await api("/critic/assessment");
    const a = r.assessment;
    if (!a) {
      el.innerHTML = `<div class="empty">Critic hasn't run yet. First assessment within 5 minutes of startup.</div>`;
      return;
    }
    const overall = a.overall_severity || "ok";
    const head = `<div class="row-spread"><div><span class="pill ${overall}">${overall.toUpperCase()}</span> ${esc(a.summary)}</div><span class="muted">${fmtEastern(a.assessed_at)}</span></div>`;
    const rows = (a.checks || []).map((c) => `
      <div class="item-row">
        <span><span class="pill ${c.severity}">${c.severity}</span> ${esc(c.metric.replace(/_/g, " "))}</span>
        <span>${esc(c.note)}</span>
      </div>
    `).join("");
    el.innerHTML = head + `<div class="list" style="margin-top:0.5rem">${rows}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

async function pullVerdict(api) {
  const el = document.getElementById("weather-verdict");
  const txt = document.getElementById("weather-verdict-text");
  if (!el || !txt) return;
  try {
    const v = await api("/operator/verdict");
    el.classList.remove("ok", "warn", "crit", "neutral");
    const verdict = (v.verdict || "UNKNOWN").toUpperCase();
    if (verdict === "GO") {
      el.classList.add("ok");
    } else if (verdict === "CAUTION") {
      el.classList.add("warn");
    } else if (verdict === "NO-GO") {
      el.classList.add("crit");
    } else {
      el.classList.add("neutral");
    }
    txt.textContent = `${verdict} — ${v.reason || ""}`;
  } catch (e) {
    el.classList.add("neutral");
    txt.textContent = `Error: ${e.message}`;
  }
}

// helpers --------------------------------------------------------------------

// Thresholds the dashboard uses for row shading. Match the imperial display
// of the default SafetyThresholds (warn 5°C / crit 2°C dew margin → 9°F /
// 3.6°F; warn 6.7 m/s / crit 8.9 m/s → 15 / 19.9 mph).
function severityFromRow(h) {
  if (h.dew_margin_f <= 3.6 || h.wind_speed_mph >= 19.9
      || h.cloud_cover_pct >= 85 || h.precip_in >= 0.004) {
    return "crit";
  }
  if (h.dew_margin_f <= 9 || h.wind_speed_mph >= 15
      || h.cloud_cover_pct >= 60) {
    return "warn";
  }
  return "ok";
}

function marginLabel(margin_f) {
  if (margin_f == null) return "—";
  if (margin_f <= 3.6) return `${margin_f} °F ⚠ critical`;
  if (margin_f <= 9) return `${margin_f} °F — watch`;
  return `${margin_f} °F`;
}

// Format a UTC ISO timestamp as America/New_York (auto EST/EDT) for display.
function fmtEastern(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-US", {
      timeZone: "America/New_York",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
      hour12: false, timeZoneName: "short",
    });
  } catch {
    return iso;
  }
}

function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
