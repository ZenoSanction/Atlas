// Setup tab — wizard and forms.

export async function initSetup(api) {
  await refreshStatus(api);
  await renderSystemFlags(api);
  await renderVaultForm(api);
  await renderSiteForm(api);
  await renderEquipmentForm(api);
  await renderThresholdsForm(api);
  wireCredentialForms(api);
}

async function renderSystemFlags(api) {
  const toggle = document.getElementById("sim-toggle");
  const save = document.getElementById("sim-save");
  const status = document.getElementById("sim-status");
  const statusLine = document.getElementById("system-flags-status");
  if (!toggle || !save) return;
  try {
    const r = await api("/setup/system-flags");
    toggle.checked = !!r.simulation_mode_db;
    if (statusLine) {
      const eff = r.simulation_mode_effective ? "ON (sim)" : "OFF (real)";
      const env = r.env_override_set
        ? " — env-var override active; toggle has no effect until you clear it"
        : "";
      statusLine.textContent = `Effective: ${eff}${env}`;
    }
  } catch (e) {
    if (statusLine) statusLine.textContent = `Error loading flags: ${e.message}`;
  }
  if (save.dataset.bound) return;
  save.dataset.bound = "1";
  save.addEventListener("click", async () => {
    try {
      const r = await api("/setup/system-flags", {
        method: "POST",
        body: JSON.stringify({ simulation_mode: toggle.checked }),
      });
      status.textContent = `Saved. Effective sim mode: ${r.simulation_mode_effective ? "ON" : "OFF"}.`;
      status.className = "form-status ok";
      await renderSystemFlags(api);  // refresh effective line
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
        await api("/setup/vault/init", { method: "POST",
          body: JSON.stringify({ password: pw }) });
        msg.textContent = "Vault created. ✓";
        msg.className = "cred-status ok";
        await refreshStatus(api);
        await renderVaultForm(api);
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
        await api("/setup/vault/unlock", { method: "POST",
          body: JSON.stringify({ password: pw }) });
        msg.textContent = "Unlocked. ✓";
        msg.className = "cred-status ok";
        await refreshStatus(api);
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
                      "warmup_ramp_c_per_min"]) {
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
