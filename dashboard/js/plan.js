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

    // Header strip: how many of the considered list got scheduled, what
    // the dwell floor + cap are, and how the scheduled time fits in the
    // dark window. Makes the "depth over breadth" policy visible.
    const cap = plan.max_targets_per_session ?? "?";
    const dwell = plan.min_dwell_minutes ?? "?";
    const sched = plan.scheduled_total_min;
    const dark = plan.dark_window_min;
    const overrun = !!plan.overruns_dark_window;
    const fitCls = overrun ? "form-status err"
                   : (dark && sched / dark > 0.85) ? "form-status warn"
                   : "form-status ok";
    const fitTxt = dark
      ? `Scheduled ${sched} min of ${dark} min dark`
        + (overrun ? " - OVERRUNS WINDOW (some targets won't get full dwell)"
                     : "")
      : `Scheduled ${sched ?? "?"} min (dark window unknown)`;
    const considered = plan.considered_count ?? plan.considered?.length ?? visible.length;
    const policyStrip = `
      <div class="hint" style="margin-bottom:8px">
        Policy: <b>top ${cap} targets per session, &ge; ${dwell} min each</b>
        (depth over breadth). ${considered} considered, ${visible.length} scheduled.
      </div>
      <div class="${fitCls}" style="margin-bottom:8px">${fitTxt}</div>`;

    const head = `<table class="tbl">
      <thead><tr>
        <th>#</th><th>Priority</th><th>Campaign</th><th>Workflow</th><th>Target</th>
        <th>Type</th><th>Dwell</th><th>Alt</th><th>Airmass</th><th>Mag</th>
      </tr></thead><tbody>`;
    const rows = visible.map((t, i) => {
      const dwellTxt = t.dwell_padded_from_min
        ? `${t.scheduled_for_min} min <span class="muted">(padded from ${t.dwell_padded_from_min})</span>`
        : `${t.scheduled_for_min ?? t.total_integration_min ?? "?"} min`;
      return `<tr>
        <td>${i + 1}</td>
        <td>${t.priority}</td>
        <td>${esc(t.campaign_name)}</td>
        <td><span class="pill">${t.workflow}</span></td>
        <td><strong>${esc(t.target_name)}</strong></td>
        <td>${esc(t.object_type || "")}</td>
        <td>${dwellTxt}</td>
        <td>${t.alt_deg}°</td>
        <td>${t.airmass ?? "—"}</td>
        <td>${t.magnitude ?? "—"}</td>
      </tr>`;
    }).join("");

    // Considered-but-not-scheduled list — collapsed by default so the
    // primary 4-target table stays the focus.
    const overflow = (plan.considered || []).slice(visible.length);
    const overflowSection = overflow.length ? `
      <details style="margin-top:14px">
        <summary class="muted">
          ${overflow.length} more target(s) ranked but not scheduled
          (cap = ${cap}/night)
        </summary>
        <ul style="margin-top:6px">
          ${overflow.slice(0, 20).map(t =>
            `<li>${esc(t.target_name)} <span class="muted">— priority ${t.priority}, alt ${t.alt_deg}°</span></li>`
          ).join("")}
        </ul>
      </details>` : "";

    out.innerHTML = policyStrip + head + rows + "</tbody></table>" + overflowSection;
  } catch (e) {
    out.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

async function renderCampaigns(api) {
  const container = document.getElementById("campaigns-list");

  // Bind the "+ New Campaign" button once, regardless of whether the
  // list is empty or full. Previously this lived after an early
  // `return` in the empty branch, so the button was unresponsive when
  // the campaigns list was empty — exactly the state every new install
  // ships in. The dataset guard prevents stacking multiple click
  // handlers on repeated renderPlan() calls.
  const newBtn = document.getElementById("new-campaign");
  if (newBtn && !newBtn.dataset.bound) {
    newBtn.dataset.bound = "1";
    newBtn.addEventListener("click", async () => {
      const name = prompt("Campaign name?");
      if (!name) return;
      const workflow = prompt(
        "Workflow (astrometry / photometry / exoplanet / transient / planetary / deepsky)?");
      if (!workflow) return;
      try {
        await api("/plan/campaigns", {
          method: "POST",
          body: JSON.stringify({ name, workflow, priority: 50 }),
        });
        renderPlan(api);
      } catch (e) {
        alert("Error: " + e.message);
      }
    });
  }

  // Activate / pause are wired on window for onclick="..." in the rows.
  window.activateCampaign = async (id) => {
    await api(`/plan/campaigns/${id}/activate`, { method: "POST" });
    renderPlan(api);
  };
  window.pauseCampaign = async (id) => {
    await api(`/plan/campaigns/${id}/pause`, { method: "POST" });
    renderPlan(api);
  };

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
}

function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
