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
      <div><span>Observed</span><span>${fmtTime(c.observed_at)} UTC</span></div>
      <div><span>Temperature</span><span>${c.temperature_c} °C</span></div>
      <div><span>Humidity</span><span>${c.humidity_pct} %</span></div>
      <div><span>Dew point</span><span>${c.dew_point_c} °C</span></div>
      <div><span>Dew margin</span><span>${marginLabel(c.dew_margin_c)}</span></div>
      <div><span>Wind</span><span>${c.wind_speed_ms} m/s${c.wind_gust_ms != null ? ` (gust ${c.wind_gust_ms})` : ""}</span></div>
      <div><span>Cloud cover</span><span>${c.cloud_cover_pct} %</span></div>
      <div><span>Precip (last hr)</span><span>${c.precip_mm} mm</span></div>
      <div><span>Pressure</span><span>${c.pressure_hpa} hPa</span></div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

async function pullForecast(api) {
  const el = document.getElementById("weather-forecast");
  if (!el) return;
  try {
    const f = await api("/weather/forecast?hours=12");
    if (!f.hourly || f.hourly.length === 0) {
      el.innerHTML = `<div class="empty">No forecast data.</div>`;
      return;
    }
    const head = `
      <table class="tbl">
        <thead><tr>
          <th>Time (UTC)</th>
          <th>Temp °C</th>
          <th>Humidity</th>
          <th>Dew margin</th>
          <th>Wind m/s</th>
          <th>Gust m/s</th>
          <th>Cloud</th>
          <th>Precip mm</th>
        </tr></thead><tbody>`;
    const rows = f.hourly.map((h) => {
      const cls = severityFromRow(h);
      return `<tr class="row-${cls}">
        <td>${fmtTime(h.time_utc)}</td>
        <td>${h.temperature_c}</td>
        <td>${h.humidity_pct}%</td>
        <td>${marginLabel(h.dew_margin_c)}</td>
        <td>${h.wind_speed_ms}</td>
        <td>${h.wind_gust_ms ?? "—"}</td>
        <td>${h.cloud_cover_pct}%</td>
        <td>${h.precip_mm}</td>
      </tr>`;
    }).join("");
    el.innerHTML = head + rows + "</tbody></table>";
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
    const head = `<div class="row-spread"><div><span class="pill ${overall}">${overall.toUpperCase()}</span> ${esc(a.summary)}</div><span class="muted">${fmtTime(a.assessed_at)} UTC</span></div>`;
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

function severityFromRow(h) {
  if (h.dew_margin_c <= 2 || h.wind_speed_ms >= 8.9 || h.cloud_cover_pct >= 85 || h.precip_mm >= 0.1) {
    return "crit";
  }
  if (h.dew_margin_c <= 5 || h.wind_speed_ms >= 6.7 || h.cloud_cover_pct >= 60) {
    return "warn";
  }
  return "ok";
}

function marginLabel(margin) {
  if (margin == null) return "—";
  if (margin <= 2) return `${margin} °C ⚠ critical`;
  if (margin <= 5) return `${margin} °C — watch`;
  return `${margin} °C`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const t = new Date(iso);
    // YYYY-MM-DD HH:MM (UTC)
    const p = (n) => String(n).padStart(2, "0");
    return `${t.getUTCFullYear()}-${p(t.getUTCMonth() + 1)}-${p(t.getUTCDate())} ${p(t.getUTCHours())}:${p(t.getUTCMinutes())}`;
  } catch {
    return iso;
  }
}

function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
