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

    // Header strip: cap, dwell floor, scheduled vs window. The scheduler
    // is depth-first and time-aware, so it auto-drops targets to keep
    // every survivor at full dwell — no overruns by construction. We
    // still show the fit because seeing "4 of 5h dark used" is the
    // operator's confirmation that the window is being filled well.
    const cap = plan.max_targets_per_session ?? "?";
    const dwell = plan.min_dwell_minutes ?? "?";
    const sched = plan.scheduled_total_min;
    const dark = plan.dark_window_min;
    const fitRatio = dark ? sched / dark : 0;
    const fitCls = fitRatio < 0.50 ? "form-status warn"
                   : "form-status ok";
    const fitTxt = dark
      ? `Filling ${sched} min of ${dark} min astronomical dark`
        + (fitRatio < 0.50
            ? ` — under-using the window; add campaigns or extend per-target dwell`
            : "")
      : `Scheduled ${sched ?? "?"} min (dark window unknown)`;
    const considered = plan.considered_count ?? plan.considered?.length ?? visible.length;
    const policyStrip = `
      <div class="hint" style="margin-bottom:8px">
        Policy: <b>queue by rise-time, cap ${cap} targets, &ge; ${dwell} min each</b>
        — depth over breadth, auto-trims to fit.
        ${considered} considered, <b>${visible.length} scheduled</b>.
      </div>
      <div class="${fitCls}" style="margin-bottom:8px">${fitTxt}</div>`;

    const head = `<table class="tbl">
      <thead><tr>
        <th>Slot</th><th>Start - End (EDT)</th><th>Dwell</th>
        <th>Target</th><th>Campaign</th><th>Workflow</th>
        <th>Visible window</th><th>Peak alt</th><th>Priority</th>
      </tr></thead><tbody>`;
    const rows = visible.map((t, i) => {
      const dwellTxt = t.scheduled_truncated_from_min
        ? `${t.scheduled_for_min} min <span class="muted">(cut from ${t.scheduled_truncated_from_min})</span>`
        : `${t.scheduled_for_min ?? t.total_integration_min ?? "?"} min`;
      const visTxt = (t.visible_from_utc && t.visible_until_utc)
        ? `${fmtClock(t.visible_from_utc)} - ${fmtClock(t.visible_until_utc)}`
        : "—";
      const startEnd = (t.start_utc && t.end_utc)
        ? `${fmtClock(t.start_utc)} - ${fmtClock(t.end_utc)}`
        : "—";
      return `<tr>
        <td>${i + 1}</td>
        <td><strong>${startEnd}</strong></td>
        <td>${dwellTxt}</td>
        <td><strong>${esc(t.target_name)}</strong>
            <span class="muted">${esc(t.object_type || "")}</span></td>
        <td>${esc(t.campaign_name)}</td>
        <td><span class="pill">${t.workflow}</span></td>
        <td>${visTxt}</td>
        <td>${t.peak_alt_deg ?? t.alt_deg ?? "—"}°</td>
        <td>${t.priority}</td>
      </tr>`;
    }).join("");

    // Unscheduled list — targets the scheduler considered but couldn't
    // fit, each with a reason: "sets at 22:15 — only 27 min available",
    // "never above horizon during dark window", or "cap of 4 reached".
    const unscheduled = plan.unscheduled || [];
    const overflowSection = unscheduled.length ? `
      <details style="margin-top:14px">
        <summary class="muted">
          ${unscheduled.length} target(s) considered but not scheduled
          (depth-over-breadth)
        </summary>
        <ul style="margin-top:6px">
          ${unscheduled.slice(0, 25).map(u =>
            `<li><strong>${esc(u.target_name || "?")}</strong>
             <span class="muted">— priority ${u.priority ?? "?"}: ${esc(u.reason || "")}</span></li>`
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

// Compact h:mm AM/PM Eastern. Time-only — date is implicit (tonight).
function fmtClock(iso) {
  if (!iso) return "—";
  try {
    const safe = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + "Z";
    return new Date(safe).toLocaleTimeString("en-US", {
      timeZone: "America/New_York",
      hour: "numeric", minute: "2-digit",
      hour12: true,
    });
  } catch { return iso; }
}
