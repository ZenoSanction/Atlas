// History tab — past sessions.

function fmtEastern(iso) {
  if (!iso) return "—";
  try {
    const safe = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + "Z";
    return new Date(safe).toLocaleString("en-US", {
      timeZone: "America/New_York", hour12: true,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "numeric", minute: "2-digit", timeZoneName: "short",
    });
  } catch { return iso; }
}

export async function renderHistory(api) {
  const container = document.getElementById("sessions-list");
  try {
    const rows = await api("/history/sessions?limit=50");
    if (!rows.length) {
      container.innerHTML = `<div class="empty">No sessions yet.</div>`;
      return;
    }
    container.innerHTML = rows.map(r => `
      <div class="item">
        <div class="item-row">
          <div>
            <strong>Session #${r.id}</strong>
            <span class="pill">${r.state}</span>
            ${r.simulation ? '<span class="pill">simulation</span>' : ""}
          </div>
          <div class="hint">${fmtEastern(r.started_at)}</div>
        </div>
      </div>
    `).join("");
  } catch (e) {
    container.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}
