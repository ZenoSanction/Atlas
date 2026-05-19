// Plan tab — tonight's targets + campaigns list.

export async function renderPlan(api) {
  await renderTonightTargets(api);
  await renderCampaigns(api);
}

async function renderTonightTargets(api) {
  const out = document.getElementById("plan-tonight");
  const stamp = document.getElementById("plan-built");
  if (!out) return;
  try {
    const r = await api("/plan/tonight");
    const plan = r.plan;
    if (!plan) {
      out.innerHTML = `<div class="empty">No plan yet. The Planner builds one on startup and every 30 minutes; if you've just installed, give it a moment. Need an active campaign with targets that have RA/Dec.</div>`;
      stamp.textContent = "";
      return;
    }
    stamp.textContent = `built ${plan.built_at} (${plan.reason})`;
    const visible = plan.visible_targets || [];
    if (visible.length === 0) {
      out.innerHTML = `<div class="empty">
        Active campaigns: ${plan.active_campaigns}. Skipped (below horizon ${plan.horizon_alt_min_deg}°): ${plan.skipped_below_horizon}. Skipped (no coords): ${plan.skipped_no_coords}. Nothing visible right now.
      </div>`;
      return;
    }
    const head = `<table class="tbl">
      <thead><tr>
        <th>Priority</th><th>Campaign</th><th>Workflow</th><th>Target</th>
        <th>Type</th><th>RA</th><th>Dec</th><th>Alt</th><th>Az</th><th>Airmass</th><th>Mag</th>
      </tr></thead><tbody>`;
    const rows = visible.map(t => `<tr>
      <td>${t.priority}</td>
      <td>${esc(t.campaign_name)}</td>
      <td><span class="pill">${t.workflow}</span></td>
      <td><strong>${esc(t.target_name)}</strong></td>
      <td>${esc(t.object_type || "")}</td>
      <td>${t.ra_deg.toFixed(3)}°</td>
      <td>${t.dec_deg.toFixed(3)}°</td>
      <td>${t.alt_deg}°</td>
      <td>${t.az_deg}°</td>
      <td>${t.airmass ?? "—"}</td>
      <td>${t.magnitude ?? "—"}</td>
    </tr>`).join("");
    out.innerHTML = head + rows + "</tbody></table>";
  } catch (e) {
    out.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

async function renderCampaigns(api) {
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
