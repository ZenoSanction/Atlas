// History tab — past sessions.

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
          <div class="hint">${new Date(r.started_at).toLocaleString()}</div>
        </div>
      </div>
    `).join("");
  } catch (e) {
    container.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}
