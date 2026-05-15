// Science tab — submission queue.

export async function renderScience(api) {
  const container = document.getElementById("submissions-list");
  try {
    const rows = await api("/science/submissions?status=queued");
    if (!rows.length) {
      container.innerHTML = `<div class="empty">No submissions awaiting review.</div>`;
      return;
    }
    container.innerHTML = rows.map(r => `
      <div class="item" data-id="${r.id}">
        <div class="item-row">
          <div>
            <span class="pill">${r.destination}</span>
            <span class="pill ${r.status === "queued" ? "warning" : ""}">${r.status}</span>
            <strong>Measurement #${r.measurement_id}</strong>
          </div>
          <div class="hint">${new Date(r.queued_at).toLocaleString()}</div>
        </div>
        <pre class="hint" style="margin-top:8px;white-space:pre-wrap;max-height:160px;overflow:auto">${esc(r.formatted_payload || "(no payload yet — will be generated when approved)")}</pre>
        <div class="actions">
          <button class="approve" onclick="window.approveSub(${r.id})">Approve</button>
          <button class="reject" onclick="window.rejectSub(${r.id})">Reject</button>
        </div>
      </div>
    `).join("");
  } catch (e) {
    container.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }

  window.approveSub = async (id) => {
    const notes = prompt("Approval notes (optional):") || "";
    await api(`/science/submissions/${id}/action`, { method: "POST",
      body: JSON.stringify({ action: "approve", notes }) });
    renderScience(api);
  };
  window.rejectSub = async (id) => {
    const reason = prompt("Reason for rejection:");
    if (reason === null) return;
    await api(`/science/submissions/${id}/action`, { method: "POST",
      body: JSON.stringify({ action: "reject", reason }) });
    renderScience(api);
  };
}

function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
