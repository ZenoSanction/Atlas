// Morning report preview panel — GET /api/reports/morning on demand.
export async function initMorningReport(api) {
  const btn = document.getElementById("morning-report-load");
  const body = document.getElementById("morning-report-body");
  if (!btn || !body || btn.dataset.bound) return;
  btn.dataset.bound = "1";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    body.innerHTML = '<span class="muted">Loading latest session report…</span>';
    try {
      const r = await api("/reports/morning");
      // Render the markdown as preformatted text. (Full markdown
      // rendering can come later; for now the raw text + the
      // structured numbers above it carry the value.)
      const html = [];
      html.push(`<div class="muted" style="margin-bottom:8px">`);
      html.push(
        `Session #${r.session_id} • ${r.duration_min} min • State: <b>${escapeHtml(
          r.state
        )}</b>${r.simulation ? " • SIMULATION" : ""}`
      );
      html.push("</div>");
      const counters = [];
      counters.push(`Autofocus: ${r.autofocus_runs}`);
      counters.push(`Plate-solves: ${r.platesolve_runs}`);
      counters.push(`Recoveries: ${(r.recovery_events || []).length}`);
      counters.push(`Alerts: ${(r.alerts || []).length} (${r.critical_alerts} critical)`);
      html.push(`<div class="muted" style="margin-bottom:8px">${counters.join(" • ")}</div>`);

      if ((r.targets || []).length) {
        html.push("<table class=\"kv-table\" style=\"margin-bottom:12px\">");
        html.push("<tr><th>Target</th><th>Frames</th><th>Minutes</th></tr>");
        for (const t of r.targets) {
          html.push(
            `<tr><td>${escapeHtml(t.target_name)}</td>` +
              `<td>${t.frames_total}</td>` +
              `<td>${t.minutes_total}</td></tr>`
          );
        }
        html.push("</table>");
      }
      html.push("<details><summary>Full markdown report</summary>");
      html.push(`<pre style="white-space:pre-wrap;max-height:400px;overflow:auto">${escapeHtml(r.markdown || "")}</pre>`);
      html.push("</details>");
      body.innerHTML = html.join("");
    } catch (e) {
      const msg = String(e);
      if (msg.includes("404")) {
        body.innerHTML =
          '<span class="muted">No COMPLETE session yet. The report fills in after the first session-stop.</span>';
      } else {
        body.innerHTML = `<span class="err">Error loading: ${escapeHtml(msg)}</span>`;
      }
    } finally {
      btn.disabled = false;
    }
  });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
