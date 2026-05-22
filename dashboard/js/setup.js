// Setup tab — wizard and forms.

import { refreshMissionControl } from "/static/js/mission-control.js";
import { renderCalibrationCoverage } from "/static/js/calibration-coverage.js";
import { initMorningReport } from "/static/js/morning-report.js";

function nudgeMissionControl() {
  // Re-fetch /api/mission-control immediately so the Tonight tab's
  // verdict banner + Session Readiness panel reflect the change
  // (vault unlock, sim toggle, etc.) without waiting for the next poll.
  try {
    if (window.atlas && window.atlas.api) refreshMissionControl(window.atlas.api);
  } catch {}
}

export async function initSetup(api) {
  await refreshStatus(api);
  await renderSystemFlags(api);
  await renderBenchSeed(api);
  await renderTlsPanel(api);
  await renderVaultForm(api);
  await renderSiteForm(api);
  await renderEquipmentForm(api);
  await renderThresholdsForm(api);
  wireCredentialForms(api);
  // New panels for the calibration library + last-session preview
  await renderCalibrationCoverage(api);
  await initMorningReport(api);
}

async function renderBenchSeed(api) {
  const btn = document.getElementById("bench-seed-btn");
  const status = document.getElementById("bench-seed-status");
  if (!btn || btn.dataset.bound) return;
  btn.dataset.bound = "1";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    status.textContent = "Seeding…";
    status.className = "form-status";
    try {
      const r = await api("/setup/seed-bench-campaign", { method: "POST" });
      const parts = [];
      if (r.created_campaign) parts.push("campaign created");
      if (r.added_targets?.length) parts.push(`added: ${r.added_targets.join(", ")}`);
      if (r.already_linked_targets?.length)
        parts.push(`already linked: ${r.already_linked_targets.join(", ")}`);
      status.textContent = `${parts.join("; ")}. ` +
                            `Total targets in campaign: ${r.total_targets}. ` +
                            `Planner will pick it up on next rebuild.`;
      status.className = "form-status ok";
      nudgeMissionControl();
    } catch (e) {
      status.textContent = `Error: ${e.message || e}`;
      status.className = "form-status err";
    } finally {
      btn.disabled = false;
    }
  });
}

// HTTPS / self-signed cert panel ---------------------------------------------
//
// The warm-room laptop / phone needs HTTPS to access the microphone (Chrome
// gates getUserMedia on raw http://lan-ip origins). This panel lets the
// operator:
//   - see the URL set their device should visit
//   - read off the cert's SHA-256 fingerprint to verify it matches what
//     the browser shows on the "this connection is not private" page
//   - regenerate the cert after the observatory PC's IP changes
async function renderTlsPanel(api) {
  const box = document.getElementById("tls-panel-body");
  if (!box) return;
  try {
    const t = await api("/setup/tls");
    const onHttps = window.location.protocol === "https:";
    const reachableUrls = (t.local_ips || [])
      .filter((ip) => ip !== "0.0.0.0")
      .map((ip) => `https://${ip}:${window.location.port || 5000}`);

    if (!t.cert_present || !t.cert) {
      box.innerHTML = `
        <p class="hint">No TLS cert on disk yet. Click <b>Regenerate</b>
        below, then restart ATLAS with HTTPS enabled
        (<code>start_atlas.bat</code> does this by default).</p>
        <button id="tls-regen" class="btn-primary" type="button">Generate cert</button>
        <span id="tls-status" class="form-status"></span>`;
    } else {
      const c = t.cert;
      const sanList = (c.subject_alt_names || []).join(", ");
      const expiryClass = c.days_until_expiry < 30 ? "form-status err"
                          : c.days_until_expiry < 90 ? "form-status warn"
                          : "form-status ok";
      const httpsStripe = onHttps
        ? `<div class="form-status ok">HTTPS active — this page is served securely.</div>`
        : `<div class="form-status warn">This page loaded over HTTP. The mic
            won't work from LAN devices until you restart ATLAS with HTTPS
            enabled (the bundled <code>start_atlas.bat</code> does this).</div>`;
      box.innerHTML = `
        ${httpsStripe}
        <p class="hint">Reach the dashboard from a warm-room device:</p>
        <ul class="tls-urls">
          ${reachableUrls.map((u) => `<li><code>${u}</code></li>`).join("")}
        </ul>
        <p class="hint">First visit on each device will show
          "Your connection is not private" — click <b>Advanced → Proceed</b>
          once. To verify you're trusting the right certificate, compare
          the SHA-256 fingerprint shown in that warning to:</p>
        <div class="tls-fp"><code>${c.fingerprint_sha256}</code></div>
        <dl class="tls-meta">
          <dt>Covers</dt><dd>${esc(sanList)}</dd>
          <dt>Valid from</dt><dd>${fmtDate(c.not_before)}</dd>
          <dt>Valid until</dt>
          <dd>${fmtDate(c.not_after)}
              <span class="${expiryClass}">(${c.days_until_expiry} days)</span></dd>
          <dt>Cert file</dt><dd><code>${esc(c.cert_path)}</code></dd>
        </dl>
        <button id="tls-regen" class="btn-secondary" type="button">Regenerate cert</button>
        <span id="tls-status" class="form-status"></span>
        <p class="hint" style="margin-top:8px">
          Regenerate after the observatory PC's IP address changes so the
          cert's SAN list catches up. New cert takes effect on the next
          ATLAS restart, and each warm-room device will need to
          re-Accept the new fingerprint once.</p>`;
    }

    const btn = document.getElementById("tls-regen");
    const status = document.getElementById("tls-status");
    if (btn) {
      btn.onclick = async () => {
        if (!confirm("Regenerate the self-signed TLS cert?\n\n" +
                      "Every device that has accepted the current cert will " +
                      "see the security warning again on next visit (and " +
                      "will need to click Advanced → Proceed).\n\n" +
                      "The new cert takes effect after ATLAS restarts.")) {
          return;
        }
        try {
          const r = await api("/setup/tls/regenerate", { method: "POST" });
          status.textContent = "Regenerated. " + r.note;
          status.className = "form-status ok";
          await renderTlsPanel(api);
        } catch (e) {
          status.textContent = e.message;
          status.className = "form-status err";
        }
      };
    }
  } catch (e) {
    box.innerHTML = `<span class="form-status err">Error: ${e.message}</span>`;
  }
}

function fmtDate(iso) {
  if (!iso) return "—";
  try {
    const safe = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + "Z";
    return new Date(safe).toLocaleString("en-US", {
      timeZone: "America/New_York", hour12: true,
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit",
    });
  } catch { return iso; }
}

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;")
                       .replace(/</g, "&lt;")
                       .replace(/>/g, "&gt;");
}

async function renderSystemFlags(api) {
  const toggle = document.getElementById("sim-toggle");
  const autoToggle = document.getElementById("auto-start-toggle");
  const save = document.getElementById("sim-save");
  const status = document.getElementById("sim-status");
  const statusLine = document.getElementById("system-flags-status");
  if (!toggle || !save) return;
  try {
    const r = await api("/setup/system-flags");
    toggle.checked = !!r.simulation_mode_db;
    if (autoToggle) autoToggle.checked = !!r.auto_start_sessions;
    if (statusLine) {
      const eff = r.simulation_mode_effective ? "ON (sim)" : "OFF (real)";
      const env = r.env_override_set
        ? " — env-var override active; toggle has no effect until you clear it"
        : "";
      const auto = r.auto_start_sessions
        ? " · auto-start: ENABLED" : " · auto-start: off";
      statusLine.textContent = `Effective: ${eff}${env}${auto}`;
    }
  } catch (e) {
    if (statusLine) statusLine.textContent = `Error loading flags: ${e.message}`;
  }
  if (save.dataset.bound) return;
  save.dataset.bound = "1";
  save.addEventListener("click", async () => {
    try {
      const body = { simulation_mode: toggle.checked };
      if (autoToggle) body.auto_start_sessions = autoToggle.checked;
      const r = await api("/setup/system-flags", {
        method: "POST", body: JSON.stringify(body),
      });
      const autoLabel = r.auto_start_sessions ? "auto-start ON" : "auto-start off";
      status.textContent = `Saved. Sim: ${r.simulation_mode_effective ? "ON" : "OFF"}, ${autoLabel}.`;
      status.className = "form-status ok";
      await renderSystemFlags(api);
      nudgeMissionControl();
    } catch (e) {
      status.textContent = e.message;
      status.className = "form-status err";
    }
  });
}

async function renderThresholdsForm(api) {
  const form = document.getElementById("thresholds-form");
  if (!form) return;
  try {
    const cur = await api("/setup/weather-thresholds");
    // Server now returns imperial keys directly; form fields match by name.
    for (const [k, v] of Object.entries(cur)) {
      const el = form.elements.namedItem(k);
      if (el && v !== null && v !== undefined) el.value = v;
    }
  } catch {}
  form.onsubmit = async (e) => {
    e.preventDefault();
    const status = form.querySelector(".form-status");
    const fd = new FormData(form);
    const data = {};
    for (const [k, v] of fd.entries()) {
      if (v === "" || v === undefined) continue;
      data[k] = Number(v);
    }
    try {
      await api("/setup/weather-thresholds", { method: "POST",
        body: JSON.stringify(data) });
      status.textContent = "Saved — Critic picks up new values on its next tick. ✓";
      status.className = "form-status ok";
    } catch (err) {
      status.textContent = err.message; status.className = "form-status err";
    }
  };
}

async function refreshStatus(api) {
  const el = document.getElementById("setup-status");
  try {
    const s = await api("/setup/status");
    const row = (label, ok) =>
      `<div class="item"><span class="${ok ? "check" : "cross"}">${ok ? "✓" : "✗"}</span> ${label}</div>`;
    el.innerHTML = `
      ${row("Master password set", s.vault_initialised)}
      ${row("Site configured", s.site_configured)}
      ${row("Equipment configured", s.equipment_configured)}
      ${row("Anthropic API key stored", s.anthropic_key_set)}
      ${row("Notifications configured", s.notifications_configured)}
    `;
  } catch (e) {
    el.innerHTML = `<span class="cross">Error: ${e.message}</span>`;
  }
}

async function renderVaultForm(api) {
  const wrap = document.getElementById("vault-form");
  let status;
  try {
    status = await api("/setup/status");
  } catch (e) {
    wrap.innerHTML = `<span class="cred-status err">Error: ${e.message}</span>`;
    return;
  }
  if (!status.vault_initialised) {
    wrap.innerHTML = `
      <p class="hint">First time setup — create your master password.</p>
      <input type="password" id="vault-new" placeholder="At least 8 characters">
      <button class="btn-primary" id="vault-init">Create</button>
      <span id="vault-msg" class="cred-status"></span>
    `;
    document.getElementById("vault-init").onclick = async () => {
      const pw = document.getElementById("vault-new").value;
      const msg = document.getElementById("vault-msg");
      try {
        const r = await api("/setup/vault/init", { method: "POST",
          body: JSON.stringify({ password: pw }) });
        msg.textContent = `Vault created. Verdict now: ${r.preflight?.verdict || "—"} ✓`;
        msg.className = "cred-status ok";
        await refreshStatus(api);
        await renderVaultForm(api);
        nudgeMissionControl();
      } catch (e) {
        msg.textContent = e.message;
        msg.className = "cred-status err";
      }
    };
  } else {
    wrap.innerHTML = `
      <p class="hint">Unlock the vault to read or set credentials.</p>
      <input type="password" id="vault-unlock-pw" placeholder="Master password">
      <button class="btn-primary" id="vault-unlock-btn">Unlock</button>
      <span id="vault-msg" class="cred-status"></span>
    `;
    document.getElementById("vault-unlock-btn").onclick = async () => {
      const pw = document.getElementById("vault-unlock-pw").value;
      const msg = document.getElementById("vault-msg");
      try {
        const r = await api("/setup/vault/unlock", { method: "POST",
          body: JSON.stringify({ password: pw }) });
        const v = r.preflight?.verdict || "—";
        msg.textContent = `Unlocked. Verdict now: ${v} ✓`;
        msg.className = "cred-status ok";
        await refreshStatus(api);
        // Kick the Tonight tab to pick up the fresh verdict without
        // waiting for its 3 s poll cycle. Without this the operator
        // unlocks, switches to Tonight, and still sees NO-GO for
        // another two seconds — feels broken even though the API
        // already responded with the new verdict.
        nudgeMissionControl();
      } catch (e) {
        msg.textContent = e.message;
        msg.className = "cred-status err";
      }
    };
  }
}

async function renderSiteForm(api) {
  const form = document.getElementById("site-form");
  const M_PER_FT = 0.3048;
  try {
    const cur = await api("/setup/site");
    if (cur) {
      for (const [k, v] of Object.entries(cur)) {
        // Display elevation in feet even though it's stored in meters
        if (k === "elevation_m") {
          const ft = form.elements.namedItem("elevation_ft");
          if (ft && v !== null && v !== undefined) ft.value = Math.round(v / M_PER_FT);
          continue;
        }
        const el = form.elements.namedItem(k);
        if (el && v !== null) el.value = v;
      }
    }
  } catch {}
  form.onsubmit = async (e) => {
    e.preventDefault();
    const status = form.querySelector(".form-status");
    const fd = Object.fromEntries(new FormData(form).entries());
    // Convert feet -> meters for storage
    if (fd.elevation_ft !== "" && fd.elevation_ft !== undefined) {
      fd.elevation_m = Number(fd.elevation_ft) * M_PER_FT;
    }
    delete fd.elevation_ft;
    const data = fd;
    for (const k of ["latitude","longitude","elevation_m","horizon_alt_min"]) {
      if (data[k] !== "" && data[k] !== undefined) data[k] = Number(data[k]);
    }
    try {
      await api("/setup/site", { method: "PUT", body: JSON.stringify(data) });
      status.textContent = "Saved. ✓"; status.className = "form-status ok";
      refreshStatus(api);
    } catch (err) {
      status.textContent = err.message; status.className = "form-status err";
    }
  };
}

async function renderEquipmentForm(api) {
  const form = document.getElementById("equipment-form");
  try {
    const cur = await api("/setup/equipment");
    if (cur) {
      for (const [k, v] of Object.entries(cur)) {
        const el = form.elements.namedItem(k);
        if (!el) continue;
        if (el.type === "checkbox") el.checked = !!v;
        else if (v !== null) el.value = v;
      }
      if (cur.filters && Array.isArray(cur.filters)) {
        form.elements.namedItem("filters_csv").value = cur.filters.join(",");
      }
    }
  } catch {}
  form.onsubmit = async (e) => {
    e.preventDefault();
    const status = form.querySelector(".form-status");
    const fd = new FormData(form);
    const data = Object.fromEntries(fd.entries());
    data.mount_supports_nonsidereal = form.elements.namedItem("mount_supports_nonsidereal").checked;
    const csv = (data.filters_csv || "").trim();
    data.filters = csv ? csv.split(",").map(s => s.trim()).filter(Boolean) : null;
    delete data.filters_csv;
    for (const k of ["sensor_pixel_size_um","focal_length_mm","aperture_mm",
                      "nina_port","phd2_port","cooling_setpoint_c",
                      "warmup_ramp_c_per_min","meridian_past_limit_min",
                      "park_alt_deg","park_az_deg","park_tolerance_deg"]) {
      if (data[k] !== "" && data[k] !== undefined) data[k] = Number(data[k]);
    }
    try {
      await api("/setup/equipment", { method: "PUT", body: JSON.stringify(data) });
      status.textContent = "Saved. ✓"; status.className = "form-status ok";
      refreshStatus(api);
    } catch (err) {
      status.textContent = err.message; status.className = "form-status err";
    }
  };
}

function wireCredentialForms(api) {
  document.querySelectorAll(".cred-form").forEach((form) => {
    const key = form.dataset.key;
    const desc = form.dataset.desc;
    const valueEl = form.querySelector(".cred-value");
    const status = form.querySelector(".cred-status");
    const btn = form.querySelector(".cred-save");
    btn.onclick = async (e) => {
      e.preventDefault();
      const val = valueEl.value.trim();
      if (!val) { status.textContent = "Value required."; status.className = "cred-status err"; return; }
      try {
        await api("/setup/credentials", {
          method: "POST",
          body: JSON.stringify({ key, value: val, description: desc }),
        });
        status.textContent = "Saved. ✓"; status.className = "cred-status ok";
        valueEl.value = "";
      } catch (err) {
        status.textContent = err.message; status.className = "cred-status err";
      }
    };
  });
}
