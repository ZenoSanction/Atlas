// Plan tab — campaigns list.

export async function renderPlan(api) {
  const container = document.getElementById("campaigns-list");
  try {
    const rows = await api("/plan/campaigns");
    if (!rows.length) {
      container.innerHTML = `<div class="empty">No campaigns yet. Click "+ New Campaign" to start one.</div>`;
      return;
    }
    container.innerHTML = rows.map(r => `
      <div class="item">
        <div class="item-row">
          <div>
            <span class="pill">${r.workflow}</span>
            <span class="pill ${r.status === "active" ? "ok" : ""}">${r.status}</span>
            <strong>${esc(r.name)}</strong>
          </div>
          <div>Priority ${r.priority}</div>
        </div>
        ${r.scientific_context ? `<div class="hint" style="margin-top:6px">${esc(r.scientific_context)}</div>` : ""}
        <div class="actions">
          ${r.status !== "active" ? `<button onclick="window.activateCampaign(${r.id})">Activate</button>` : ""}
          ${r.status === "active" ? `<button onclick="window.pauseCampaign(${r.id})">Pause</button>` : ""}
        </div>
      </div>
    `).join("");
  } catch (e) {
    container.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }

  window.activateCampaign = async (id) => {
    await api(`/plan/campaigns/${id}/activate`, { method: "POST" });
    renderPlan(api);
  };
  window.pauseCampaign = async (id) => {
    await api(`/plan/campaigns/${id}/pause`, { method: "POST" });
    renderPlan(api);
  };

  document.getElementById("new-campaign").onclick = async () => {
    const name = prompt("Campaign name?");
    if (!name) return;
    const workflow = prompt("Workflow (astrometry / photometry / exoplanet / transient / planetary / deepsky)?");
    if (!workflow) return;
    try {
      await api("/plan/campaigns", { method: "POST",
        body: JSON.stringify({ name, workflow, priority: 50 }) });
      renderPlan(api);
    } catch (e) {
      alert("Error: " + e.message);
    }
  };
}

function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
