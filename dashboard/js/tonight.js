// Tonight tab — live session view.

export function renderTonight(api) { refreshTonight(api); }

export async function refreshTonight(api) {
  let s;
  try {
    s = await api("/tonight/status");
  } catch (e) {
    document.getElementById("gonogo-text").textContent = `Error: ${e.message}`;
    return;
  }
  const banner = document.getElementById("gonogo");
  const txt = document.getElementById("gonogo-text");

  // GO/NO-GO from alerts
  const alerts = s.alerts || [];
  const crit = alerts.find(a => a.severity === "critical");
  const warn = alerts.find(a => a.severity === "warning");
  banner.classList.remove("ok", "warn", "crit", "neutral");
  if (crit) {
    banner.classList.add("crit");
    txt.textContent = `NO-GO — ${crit.message}`;
  } else if (warn) {
    banner.classList.add("warn");
    txt.textContent = `CAUTION — ${warn.message}`;
  } else if (s.session && (s.session.state === "nominal" || s.session.state === "pre_session")) {
    banner.classList.add("ok");
    txt.textContent = "GO — all systems nominal.";
  } else {
    banner.classList.add("neutral");
    txt.textContent = s.session ? `Session ${s.session.state}` : "No active session.";
  }

  // Session card
  const sessEl = document.getElementById("session-info");
  if (s.session) {
    sessEl.innerHTML = `
      <div><span>ID</span><span>${s.session.id}</span></div>
      <div><span>State</span><span>${s.session.state}</span></div>
      <div><span>Started</span><span>${new Date(s.session.started_at).toLocaleString()}</span></div>
      <div><span>Mode</span><span>${s.session.simulation ? "SIMULATION" : "live"}</span></div>
    `;
  } else {
    sessEl.innerHTML = `<div class="empty">No session yet.</div>`;
  }

  // Hardware card
  const hw = s.hardware || {};
  const hwLabel = (item) => {
    if (!item) return "—";
    if (item.connected) {
      let detail = "";
      if (item.temperature !== undefined && item.temperature !== null) detail = `${item.temperature.toFixed(1)}°C`;
      if (item.position !== undefined && item.position !== null) detail = `pos ${item.position}`;
      if (item.current_filter) detail = item.current_filter;
      if (item.parked !== undefined) detail = item.parked ? "parked" : (item.tracking ? "tracking" : "idle");
      if (item.state) detail = item.state;
      return `<span style="color:var(--ok)">●</span> ${detail || "ok"}`;
    }
    return `<span style="color:var(--text-dim)">○</span> ${esc(item.status || "offline")}`;
  };
  document.getElementById("hardware-info").innerHTML = `
    <div><span>Camera</span><span>${hwLabel(hw.camera)}</span></div>
    <div><span>Mount</span><span>${hwLabel(hw.mount)}</span></div>
    <div><span>Focuser (EAF)</span><span>${hwLabel(hw.focuser)}</span></div>
    <div><span>Filter wheel</span><span>${hwLabel(hw.filterwheel)}</span></div>
    <div><span>Guiding (PHD2)</span><span>${hwLabel(hw.guiding)}</span></div>
  `;

  // Agents card
  const ag = s.agents || {};
  document.getElementById("agents-info").innerHTML =
    Object.entries(ag).map(([name, info]) => `
      <div>
        <span>${name}</span>
        <span>${info.running ? "● running" : "○ stopped"}${info.safe_mode ? " (safe-mode)" : ""}</span>
      </div>`).join("");

  // Disk card
  const d = s.disk || {};
  document.getElementById("disk-info").innerHTML = `
    <div><span>Free</span><span>${d.gb_free ?? "?"} GB</span></div>
    <div><span>Used</span><span>${d.percent_used ?? "?"} %</span></div>
  `;

  // Alerts
  const aEl = document.getElementById("alerts-info");
  if (!alerts.length) {
    aEl.innerHTML = `<div class="empty">No active alerts.</div>`;
  } else {
    aEl.innerHTML = alerts.map(a => `
      <div class="item-row">
        <span><span class="pill ${a.severity}">${a.severity}</span> ${esc(a.code)}: ${esc(a.message)}</span>
      </div>`).join("");
  }
}

function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
