// Multi-night campaign progress panel.
// GET /api/campaigns/progress → renders per-campaign bars.
export async function renderCampaignProgress(api) {
  const body = document.getElementById("campaign-progress-body");
  const btn = document.getElementById("campaign-progress-refresh");
  if (!body) return;
  if (btn && !btn.dataset.bound) {
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => renderCampaignProgress(api));
  }
  body.textContent = "Loading…";
  try {
    const r = await api("/campaigns/progress");
    const campaigns = r.campaigns || [];
    if (!campaigns.length) {
      body.innerHTML =
        '<span class="muted">No active campaigns. Create one on this tab or via the Setup-tab "Bench-Test Campaign" seed button.</span>';
      return;
    }
    const html = [];
    for (const c of campaigns) {
      const doneBadge = c.is_done
        ? '<span class="ok-pill">DONE</span>'
        : `<span class="muted">${c.complete_pct.toFixed(1)}%</span>`;
      html.push('<div class="card" style="margin-bottom:12px">');
      html.push(
        `<div class="row-spread"><h4 style="margin:0">${escapeHtml(
          c.campaign_name
        )}</h4>${doneBadge}</div>`
      );
      html.push(`<div class="muted" style="margin-bottom:8px">${escapeHtml(c.summary)}</div>`);

      // Stacked bar by filter
      const required =
        (c.success_criterion && c.success_criterion.min_minutes_per_filter) || {};
      if (Object.keys(required).length) {
        const accum = {};
        for (const t of c.targets || []) {
          for (const [filt, mins] of Object.entries(t.minutes_per_filter || {})) {
            accum[filt] = (accum[filt] || 0) + mins;
          }
        }
        html.push('<table class="kv-table" style="margin-bottom:8px">');
        html.push("<tr><th>Filter</th><th>Have</th><th>Need</th><th>Progress</th></tr>");
        for (const [filt, need] of Object.entries(required)) {
          const have = accum[filt.toUpperCase()] || 0;
          const pct = Math.min(100, (have / need) * 100);
          html.push("<tr>");
          html.push(`<td><b>${escapeHtml(filt)}</b></td>`);
          html.push(`<td>${have.toFixed(1)} min</td>`);
          html.push(`<td>${need} min</td>`);
          html.push(
            `<td><div class="progress-bar"><div class="progress-fill" style="width:${pct.toFixed(
              1
            )}%"></div></div> ${pct.toFixed(0)}%</td>`
          );
          html.push("</tr>");
        }
        html.push("</table>");
      }

      // Per-target list
      if ((c.targets || []).length) {
        html.push('<details><summary>Targets in this campaign</summary>');
        html.push('<table class="kv-table" style="margin-top:6px">');
        html.push("<tr><th>Target</th><th>Total</th><th>Sessions</th><th>Last visit</th></tr>");
        for (const t of c.targets) {
          html.push(
            `<tr><td>${escapeHtml(t.target_name)}</td>` +
              `<td>${t.total_minutes.toFixed(1)} min (${t.total_frames} frames)</td>` +
              `<td>${t.n_sessions}</td>` +
              `<td>${t.last_visit_utc || "—"}</td></tr>`
          );
        }
        html.push("</table></details>");
      }
      html.push("</div>");
    }
    body.innerHTML = html.join("");
  } catch (e) {
    body.innerHTML = `<span class="err">Error loading campaign progress: ${escapeHtml(String(e))}</span>`;
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
