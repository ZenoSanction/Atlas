// Right Now — unified live snapshot polled from /api/right-now.
//
// Doctrine: same data, same view, same answer for ATLAS the LLM, the
// dashboard, and any other agent. This module renders the three-layer
// snapshot at the top of every tab and the Pending Decisions panel
// beneath it. See docs/operational_awareness.md.

const POLL_MS = 5000;   // 5 s — Right Now is cheap to compute
let _timer = null;

function fmt(v, dash = "—") {
  if (v === null || v === undefined || v === "") return dash;
  return v;
}

function pct(v) {
  if (v === null || v === undefined) return "—";
  return `${v}%`;
}

function pillForVerdict(v) {
  const cls = v === "GO" ? "good" : v === "NO-GO" ? "bad" : v === "CAUTION" ? "warn" : "muted";
  return `<span class="rn-pill ${cls}">${v || "UNKNOWN"}</span>`;
}

function pillForSev(s) {
  if (!s) return "";
  const cls = s === "critical" ? "bad" : s === "warning" ? "warn" : "good";
  return `<span class="rn-pill ${cls}">${s}</span>`;
}

function renderSituational(s) {
  if (!s) return "—";
  const dp = s.day_phase || {};
  const mc = s.manual_control || {};
  const lines = [
    `${pillForVerdict(s.verdict)} <span class="muted">${fmt(s.verdict_reason, "")}</span>`,
    `Weather: ${pillForSev(s.weather_severity)} <span class="muted">${fmt(s.weather_summary, "no read yet")}</span>`,
    `Phase: <span class="mono">${fmt(dp.phase)}</span>` +
      (s.minutes_of_dark_remaining != null
        ? ` <span class="muted">(${Math.round(s.minutes_of_dark_remaining)} min dark left)</span>`
        : (s.minutes_until_next_phase != null
            ? ` <span class="muted">(next in ${Math.round(s.minutes_until_next_phase)} min)</span>`
            : "")),
  ];
  if (mc.engaged) {
    lines.push(`<span class="rn-pill warn">MANUAL</span> <span class="muted">${fmt(mc.reason, "")}</span>`);
  }
  if (s.preflight_verdict) {
    lines.push(`Preflight: <span class="mono">${s.preflight_verdict}</span>`);
  }
  return lines.map(l => `<div class="rn-line">${l}</div>`).join("");
}

function renderProcedural(p) {
  if (!p) return "—";
  const slot = p.active_slot;
  if (!slot) {
    return `<div class="rn-line muted">No slot active.${
      p.next_action ? ` Next: ${p.next_action}` : ""
    }</div>`;
  }
  const lines = [
    `<div class="rn-line">Slot: <strong>${fmt(slot.target_name)}</strong>` +
      ` <span class="muted">(${fmt(slot.workflow, "")})</span></div>`,
  ];
  if (p.active_action) lines.push(`<div class="rn-line">Doing: ${p.active_action}</div>`);
  if (p.active_frame) {
    const af = p.active_frame;
    lines.push(`<div class="rn-line muted">Frame: ${fmt(af.filter)} ${fmt(af.exposure_s)}s ${fmt(af.index)}/${fmt(af.count)}</div>`);
  }
  if (p.slot_progress) {
    const sp = p.slot_progress;
    lines.push(`<div class="rn-line muted">Progress: ${fmt(sp.frames_done)}/${fmt(sp.frames_total)} frames · ${fmt(sp.elapsed_min)}/${fmt(sp.scheduled_min)} min</div>`);
  }
  if (p.next_action) {
    lines.push(`<div class="rn-line muted">Next: ${p.next_action}${p.next_action_at ? " @ " + p.next_action_at : ""}</div>`);
  }
  if (p.planned_session_end) {
    lines.push(`<div class="rn-line muted">Planned end: ${p.planned_session_end}</div>`);
  }
  return lines.join("");
}

function renderStrategic(s) {
  if (!s) return "—";
  if (!s.plan_present) {
    return `<div class="rn-line muted">No plan published yet.</div>`;
  }
  const adv = s.advisory_counts || { info: 0, warning: 0, critical: 0 };
  const lines = [
    `<div class="rn-line">Plan: <strong>${s.visible_target_count} targets</strong>` +
      (s.fit_pct != null ? ` <span class="muted">(${pct(s.fit_pct)} fit of dark window)</span>` : "") +
      `</div>`,
    `<div class="rn-line muted">Campaigns: ${fmt(s.active_campaigns, 0)}` +
      (s.fallback_to_catalog ? " · fallback to catalog" : "") +
      (s.in_recovery ? " · in recovery" : "") +
      `</div>`,
    `<div class="rn-line">Advisories: ${pillForSev("critical")}×${adv.critical} ${pillForSev("warning")}×${adv.warning} ${pillForSev("info")}×${adv.info}</div>`,
    `<div class="rn-line muted">Review phase: <span class="mono">${fmt(s.review_phase)}</span></div>`,
  ];
  return lines.join("");
}

async function refreshRightNow() {
  try {
    const rn = await window.atlas.api("/right-now");
    const bar = document.getElementById("right-now-bar");
    if (bar) bar.dataset.state = "loaded";
    const $ = (id) => document.getElementById(id);
    if ($("rn-summary")) $("rn-summary").textContent = rn.summary || "—";
    if ($("rn-situational")) $("rn-situational").innerHTML = renderSituational(rn.situational);
    if ($("rn-procedural")) $("rn-procedural").innerHTML = renderProcedural(rn.procedural);
    if ($("rn-strategic")) $("rn-strategic").innerHTML = renderStrategic(rn.strategic);
    renderPendingDecisions(rn.pending_decisions || []);
  } catch (e) {
    const bar = document.getElementById("right-now-bar");
    if (bar) bar.dataset.state = "error";
    const sum = document.getElementById("rn-summary");
    if (sum) sum.textContent = `Right Now unavailable: ${e.message}`;
  }
}

// ---- Pending Decisions ----------------------------------------------------

function renderPendingDecisions(list) {
  const wrap = document.getElementById("pending-decisions");
  if (!wrap) return;
  if (!list || !list.length) {
    wrap.classList.add("hidden");
    wrap.innerHTML = "";
    return;
  }
  wrap.classList.remove("hidden");
  wrap.innerHTML = list.map(pd => {
    const sev = pd.severity || "info";
    const cl = pd.confidence_layer || "rules";
    return `
      <div class="pending-card" data-id="${pd.id}" data-severity="${sev}">
        <div class="pd-head">
          <span class="pd-verb mono">${pd.kind || "?"}</span>
          <span class="rn-pill ${sev === "critical" ? "bad" : sev === "warning" ? "warn" : "good"}">${sev}</span>
          <span class="muted pd-layer">layer: ${cl}</span>
          <span class="muted pd-decide-by" title="default: ${pd.default_action}">decide by ${pd.decide_by || "—"}</span>
        </div>
        <div class="pd-narration">${pd.narration || ""}</div>
        <div class="pd-actions">
          <button class="btn-primary" data-action="apply" data-id="${pd.id}">Apply</button>
          <button class="btn-secondary" data-action="override" data-id="${pd.id}">Override…</button>
          <button class="btn-link" data-action="cancel" data-id="${pd.id}">Cancel</button>
        </div>
      </div>`;
  }).join("");
}

async function onPendingClick(e) {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const id = btn.dataset.id;
  const action = btn.dataset.action;
  let body = { action };
  if (action === "override") {
    const verb = prompt("Override verb (pause/resume/drop_slot/truncate/swap/insert/safe_shutdown):");
    if (!verb) return;
    const reason = prompt("Reason for override:") || "operator override";
    body = { action: "override", verb, reason };
  }
  try {
    await window.atlas.api(`/pending-decisions/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    refreshRightNow();   // refresh immediately
  } catch (err) {
    alert(`Failed to ${action}: ${err.message}`);
  }
}

// ---- Plan version timeline (rendered on Plan tab) -------------------------

export async function refreshPlanHistory() {
  try {
    const data = await window.atlas.api("/plan/history");
    const badge = document.getElementById("plan-version-badge");
    if (badge) {
      if (data.plan_version) {
        badge.textContent = `v${data.plan_version}`;
        badge.classList.remove("hidden");
      } else {
        badge.classList.add("hidden");
      }
    }
    const list = document.getElementById("plan-history");
    if (!list) return;
    if (!data.history || !data.history.length) {
      list.innerHTML = `<li class="muted">No history yet.</li>`;
      return;
    }
    list.innerHTML = data.history.slice().reverse().map(h => {
      const arrow = h.parent_version
        ? `v${h.parent_version} → v${h.version}`
        : `v${h.version}`;
      const detailParts = [];
      const d = h.diff || {};
      if (d.dropped && d.dropped.length) detailParts.push(`dropped: ${d.dropped.join(", ")}`);
      if (d.added   && d.added.length)   detailParts.push(`added: ${d.added.join(", ")}`);
      if (d.swapped) detailParts.push(`swapped: ${(d.swapped || []).join(" → ")}`);
      if (d.truncated_at) detailParts.push(`end: ${d.truncated_at}`);
      if (d.inserted) detailParts.push(`inserted: ${d.inserted}`);
      const detail = detailParts.length ? ` <span class="muted">[${detailParts.join("; ")}]</span>` : "";
      return `<li class="ph-entry">
        <span class="mono ph-arrow">${arrow}</span>
        <span class="ph-verb">${h.verb || "change"}</span>
        <span class="ph-reason">${h.reason || ""}</span>
        ${detail}
        <span class="muted ph-at">${h.at || ""}</span>
      </li>`;
    }).join("");
  } catch (e) {
    const list = document.getElementById("plan-history");
    if (list) list.innerHTML = `<li class="bad">History unavailable: ${e.message}</li>`;
  }
}

// ---- Boot -----------------------------------------------------------------

export function initRightNow() {
  const wrap = document.getElementById("pending-decisions");
  if (wrap) wrap.addEventListener("click", onPendingClick);
  const refresh = document.getElementById("rn-refresh");
  if (refresh) refresh.addEventListener("click", refreshRightNow);

  refreshRightNow();
  if (_timer) clearInterval(_timer);
  _timer = setInterval(refreshRightNow, POLL_MS);
}
