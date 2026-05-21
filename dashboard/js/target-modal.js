// Per-campaign "add targets" modal.
//
// Operator flow:
//   1. On the Plan tab, click "+ Targets" on a campaign row.
//   2. Modal opens, focuses the search field, shows the campaign's
//      currently-linked targets at the bottom.
//   3. Operator types a name. We debounce-search the seasonal catalog
//      via /api/plan/targets/search and render hits with current alt/az.
//   4. If no catalog hit and the SIMBAD checkbox is on, we re-issue
//      with fallback_simbad=true and surface whatever SIMBAD returns.
//   5. Click "Add" on a result → POST /api/plan/campaigns/{id}/targets.
//      Result is shown inline ("Added: M51") and the already-in-
//      campaign list refreshes.
//   6. Close button or Esc dismisses; the Plan tab refreshes so the
//      campaign row's target-count is correct.

let _api = null;
let _campaignId = null;
let _campaignName = "";
let _searchTimer = null;
let _onCloseCallback = null;

const SEARCH_DEBOUNCE_MS = 220;

export function openTargetModal(api, campaign, onClose) {
  _api = api;
  _campaignId = campaign.id;
  _campaignName = campaign.name || `Campaign #${campaign.id}`;
  _onCloseCallback = onClose || null;

  const modal = document.getElementById("target-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  document.getElementById("target-modal-title").textContent =
    `Add targets to: ${_campaignName}`;
  document.getElementById("target-search-input").value = "";
  document.getElementById("target-search-simbad").checked = false;
  document.getElementById("target-search-results").innerHTML = "";
  document.getElementById("target-search-status").textContent = "";
  refreshCurrentList();
  // Initial empty-query search shows the brightest catalog entries
  // as a browse list.
  runSearch("");
  setTimeout(() => document.getElementById("target-search-input").focus(), 30);
}

function closeModal() {
  const modal = document.getElementById("target-modal");
  if (modal) modal.classList.add("hidden");
  if (_onCloseCallback) {
    const cb = _onCloseCallback;
    _onCloseCallback = null;
    try { cb(); } catch {}
  }
}

async function runSearch(query) {
  const status = document.getElementById("target-search-status");
  const out = document.getElementById("target-search-results");
  if (!status || !out) return;
  const fallback = document.getElementById("target-search-simbad")?.checked;
  status.textContent = query ? `Searching for "${query}"…` : "Top of seasonal catalog:";
  try {
    const params = new URLSearchParams();
    if (query) params.set("q", query);
    params.set("limit", "20");
    if (fallback) params.set("fallback_simbad", "true");
    const data = await _api(`/plan/targets/search?${params.toString()}`);
    const results = data.results || [];
    if (results.length === 0) {
      if (data.simbad_tried) {
        status.textContent = `Nothing in the catalog or SIMBAD for "${query}".`;
      } else if (query) {
        status.textContent =
          `No catalog hits for "${query}". `
          + `Tick "Fall back to SIMBAD" and search again to query the
             full SIMBAD database.`;
      } else {
        status.textContent = "Catalog is empty.";
      }
      out.innerHTML = "";
      return;
    }
    status.textContent =
      (query ? `Found ${results.length} match(es) for "${query}"` : "")
      + (results.some((r) => r.source === "simbad") ? " — via SIMBAD" : "");
    out.innerHTML = results.map((r) => renderResult(r)).join("");
    out.querySelectorAll("button.add-target").forEach((btn) => {
      btn.addEventListener("click", () => addTarget(btn, JSON.parse(btn.dataset.payload)));
    });
  } catch (e) {
    status.textContent = `Search failed: ${e.message || e}`;
    out.innerHTML = "";
  }
}

function renderResult(r) {
  const sourceTag = r.source === "simbad"
    ? `<span class="pill simbad">SIMBAD</span>` : "";
  const altTag = r.alt_deg !== null && r.alt_deg !== undefined
    ? (r.above_horizon_now
        ? `<span class="pill ok">up · ${r.alt_deg}°</span>`
        : `<span class="pill warn">below horizon · ${r.alt_deg}°</span>`)
    : "";
  const magTag = r.magnitude !== null && r.magnitude !== undefined
    ? `<span class="muted">mag ${r.magnitude}</span>` : "";
  const aliases = (r.alt_names || []).length
    ? `<span class="muted">(${r.alt_names.join(", ")})</span>` : "";
  const notes = r.notes ? `<div class="muted small">${esc(r.notes)}</div>` : "";
  const payload = JSON.stringify({
    name: r.name, ra_deg: r.ra_deg, dec_deg: r.dec_deg,
    magnitude: r.magnitude, object_type: r.object_type,
    alt_names: r.alt_names,
  });
  return `
    <div class="result-row">
      <div class="result-main">
        <div>
          <strong>${esc(r.name)}</strong> ${aliases}
          ${sourceTag}
        </div>
        <div class="result-meta">
          ${esc(r.object_type || "")} ${magTag} ${altTag}
        </div>
        ${notes}
      </div>
      <div class="result-actions">
        <button class="btn-primary add-target" data-payload='${esc(payload)}'>+ Add</button>
      </div>
    </div>`;
}

async function addTarget(button, payload) {
  button.disabled = true;
  button.textContent = "…";
  try {
    const r = await _api(`/plan/campaigns/${_campaignId}/targets`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (r.already_linked) {
      button.textContent = "✓ Already added";
      button.classList.add("muted");
    } else {
      button.textContent = "✓ Added";
      button.classList.add("ok");
    }
    refreshCurrentList();
  } catch (e) {
    button.disabled = false;
    button.textContent = "+ Add";
    alert(`Add failed: ${e.message || e}`);
  }
}

async function refreshCurrentList() {
  const list = document.getElementById("target-current-list");
  const count = document.getElementById("target-current-count");
  if (!list || !count) return;
  try {
    const rows = await _api(`/plan/campaigns/${_campaignId}/targets`);
    count.textContent = String(rows.length);
    if (!rows.length) {
      list.innerHTML = `<em class="muted">none yet</em>`;
      return;
    }
    list.innerHTML = rows.map((t) => `
      <div class="current-row">
        <span><strong>${esc(t.name)}</strong>
          <span class="muted">${esc(t.object_type || "")}</span>
          ${t.magnitude != null ? `<span class="muted">mag ${t.magnitude}</span>` : ""}
        </span>
        <button class="btn-link remove-target" data-tid="${t.id}">remove</button>
      </div>
    `).join("");
    list.querySelectorAll("button.remove-target").forEach((btn) => {
      btn.addEventListener("click", () => removeTarget(btn.dataset.tid));
    });
  } catch (e) {
    list.innerHTML = `<span class="form-status err">${e.message}</span>`;
  }
}

async function removeTarget(targetId) {
  try {
    await _api(`/plan/campaigns/${_campaignId}/targets/${targetId}`,
                { method: "DELETE" });
    refreshCurrentList();
  } catch (e) {
    alert(`Remove failed: ${e.message || e}`);
  }
}

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;")
                       .replace(/</g, "&lt;")
                       .replace(/>/g, "&gt;")
                       .replace(/'/g, "&apos;")
                       .replace(/"/g, "&quot;");
}

export function initTargetModal() {
  const closeBtn = document.getElementById("target-modal-close");
  if (closeBtn && !closeBtn.dataset.bound) {
    closeBtn.dataset.bound = "1";
    closeBtn.addEventListener("click", closeModal);
  }
  const overlay = document.getElementById("target-modal");
  if (overlay && !overlay.dataset.bound) {
    overlay.dataset.bound = "1";
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !overlay.classList.contains("hidden")) {
        closeModal();
      }
    });
  }
  const input = document.getElementById("target-search-input");
  if (input && !input.dataset.bound) {
    input.dataset.bound = "1";
    input.addEventListener("input", () => {
      if (_searchTimer) clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => runSearch(input.value.trim()),
                                  SEARCH_DEBOUNCE_MS);
    });
  }
  const simbad = document.getElementById("target-search-simbad");
  if (simbad && !simbad.dataset.bound) {
    simbad.dataset.bound = "1";
    simbad.addEventListener("change", () => {
      const q = document.getElementById("target-search-input").value.trim();
      runSearch(q);
    });
  }
}
