// Calibration library coverage panel.
// Calls GET /api/calibration/coverage and renders bars by kind +
// the recommended action queue.
export async function renderCalibrationCoverage(api) {
  const body = document.getElementById("calibration-coverage-body");
  const btn = document.getElementById("calibration-refresh");
  if (!body) return;
  if (btn && !btn.dataset.bound) {
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => renderCalibrationCoverage(api));
  }
  body.textContent = "Loading…";
  try {
    const r = await api("/calibration/coverage");
    const cov = r.coverage || {};
    const actions = r.actions || [];
    const html = [];

    // Summary row
    html.push(`<div class="muted" style="margin-bottom:8px"><b>${escapeHtml(cov.summary || "")}</b></div>`);

    // Per-kind counts table
    html.push('<table class="kv-table" style="margin-bottom:12px">');
    for (const kind of ["bias", "dark", "flat"]) {
      const list = cov[kind] || [];
      const stale = list.filter((m) => !m.fresh).length;
      const fresh = list.length - stale;
      html.push(
        `<tr><td><b>${kind}</b> masters</td>` +
          `<td>${list.length} total — ${fresh} fresh, ${stale} stale</td></tr>`
      );
    }
    html.push("</table>");

    // Missing combos
    if ((cov.missing || []).length) {
      html.push('<div style="margin-bottom:12px">');
      html.push('<div class="muted" style="margin-bottom:4px"><b>Missing combos:</b></div>');
      html.push('<ul style="margin:0;padding-left:20px">');
      for (const m of cov.missing.slice(0, 20)) {
        const parts = [m.kind];
        if (m.filter) parts.push(`filter=${m.filter}`);
        if (m.exposure_s !== undefined) parts.push(`${m.exposure_s}s`);
        if (m.gain !== null && m.gain !== undefined) parts.push(`gain ${m.gain}`);
        if (m.ccd_temp_c !== null && m.ccd_temp_c !== undefined) {
          parts.push(`${m.ccd_temp_c}°C`);
        }
        html.push(`<li>${escapeHtml(parts.join(", "))}</li>`);
      }
      if (cov.missing.length > 20) {
        html.push(`<li class="muted">… and ${cov.missing.length - 20} more</li>`);
      }
      html.push("</ul></div>");
    }

    // Action queue
    if (actions.length) {
      html.push('<div class="muted" style="margin-bottom:4px"><b>Recommended actions:</b></div>');
      html.push('<table class="kv-table">');
      html.push("<tr><th>Priority</th><th>Action</th></tr>");
      for (const a of actions.slice(0, 30)) {
        const badge = priorityBadge(a.priority);
        html.push(
          `<tr><td>${badge}</td><td>${escapeHtml(a.summary || "")}</td></tr>`
        );
      }
      html.push("</table>");
    } else {
      html.push('<div class="ok-pill">No missing combos — calibration library is current.</div>');
    }
    body.innerHTML = html.join("");
  } catch (e) {
    body.innerHTML = `<span class="err">Error loading calibration coverage: ${escapeHtml(String(e))}</span>`;
  }
}

function priorityBadge(p) {
  const cls = p === "high" ? "warn-pill" : p === "medium" ? "muted-pill" : "muted";
  return `<span class="${cls}">${escapeHtml(p || "")}</span>`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
