// Manual control — Take Control banner + Hardware Controls panel.
//
// The button in the topbar engages or releases manual control. When
// engaged:
//   - A red MANUAL banner is shown across every tab (sits above <main>).
//   - The Hardware Controls panel on the Tonight tab becomes visible.
//   - The Operator agent stops dispatching autonomous decisions; the
//     dashboard's hardware buttons are the only way work happens.
//
// Every manual action requires a rationale (free text). The Operator
// records each action in an audit ring buffer that the morning report
// reads at session end.

let _api = null;
let _pollTimer = null;
let _lastEngaged = null;     // local memo so we can detect transitions

const POLL_MS = 4000;

const STATUS_URL = "/control/status";

const KIND_LABEL = {
  slew: "Slew", park: "Park", unpark: "Unpark",
  capture: "Capture", set_cooling: "Set cooling", warmup: "Warmup",
  move_focuser: "Move focuser", change_filter: "Change filter",
  dome_open: "Dome open", dome_close: "Dome close",
};

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("en-US", {
      timeZone: "America/New_York", hour12: false,
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch { return iso; }
}

function setBannerVisible(engaged, mc) {
  const banner = document.getElementById("manual-banner");
  const panel  = document.getElementById("hardware-controls");
  const tc     = document.getElementById("take-control");
  if (!banner || !panel || !tc) return;

  if (engaged) {
    banner.classList.remove("hidden");
    panel.classList.remove("hidden");
    tc.classList.add("active");
    tc.textContent = "Release Control";
    const detail = document.getElementById("manual-banner-detail");
    if (detail) {
      const since = mc?.engaged_at ? fmtTime(mc.engaged_at) : "—";
      detail.textContent = `since ${since} · ${mc?.reason || "no reason given"}`;
    }
    const cnt = document.getElementById("manual-action-count");
    if (cnt) cnt.textContent = String(mc?.action_count ?? 0);
  } else {
    banner.classList.add("hidden");
    panel.classList.add("hidden");
    tc.classList.remove("active");
    tc.textContent = "Take Control";
  }
}

function renderAudit(actions) {
  const list = document.getElementById("manual-actions-list");
  const cntPill = document.getElementById("manual-actions-count");
  if (!list) return;
  if (cntPill) cntPill.textContent = String(actions?.length || 0);
  if (!actions || actions.length === 0) {
    list.innerHTML = `<em class="muted">no actions yet this session</em>`;
    return;
  }
  list.innerHTML = actions.map((a) => {
    const argsTxt = Object.entries(a.args || {})
      .map(([k, v]) => `${k}=${v}`).join(", ");
    const okCls = a.ok ? "ok" : "fail";
    const label = KIND_LABEL[a.kind] || a.kind;
    return `
      <div class="manual-action ${okCls}">
        <span class="ma-time">${fmtTime(a.at)}</span>
        <span class="ma-kind">${label}</span>
        <span class="ma-args muted">${argsTxt}</span>
        <span class="ma-reason">— ${a.rationale || ""}</span>
        <span class="ma-status ${okCls}">${a.ok ? "ok" : "FAILED"}</span>
      </div>`;
  }).join("");
}

export async function refreshManualControl() {
  if (!_api) return;
  try {
    const data = await _api(STATUS_URL);
    setBannerVisible(!!data.engaged, data);
    renderAudit(data.actions);
    _lastEngaged = !!data.engaged;
  } catch (e) {
    console.warn("refreshManualControl failed:", e?.message || e);
  }
}

async function postCommand(url, body) {
  return _api(url, { method: "POST", body: JSON.stringify(body || {}) });
}

async function takeControl() {
  const reason = window.prompt(
    "Take control of ATLAS?\n\n" +
    "Autonomy will pause: the Operator will stop dispatching session decisions, " +
    "alert auto-fixes, and pipeline hand-offs. The Hardware Controls panel on " +
    "the Tonight tab becomes the only way work happens.\n\n" +
    "Reason (required, recorded in the morning report):"
  );
  if (reason === null) return;          // cancelled
  const trimmed = (reason || "").trim();
  if (!trimmed) {
    alert("A reason is required to take control.");
    return;
  }
  try {
    await postCommand("/control/take", { reason: trimmed });
    await refreshManualControl();
  } catch (e) {
    alert("Take control failed: " + (e?.message || e));
  }
}

async function releaseControl() {
  const reason = window.prompt(
    "Release control back to the autonomous Operator?\n\n" +
    "Recommended: leave a short note on why you're stepping back " +
    "(e.g. 'done refocusing', 'crisis resolved').",
    "released"
  );
  if (reason === null) return;
  try {
    await postCommand("/control/release",
                       { reason: (reason || "released").trim() });
    await refreshManualControl();
  } catch (e) {
    alert("Release failed: " + (e?.message || e));
  }
}

async function submitFormAction(form) {
  const kind = form.dataset.kind;
  const data = new FormData(form);
  const rationale = (data.get("rationale") || "").toString().trim();
  if (!rationale) {
    alert("Every manual action requires a rationale (it goes into the audit log).");
    return;
  }
  const args = {};
  for (const [k, v] of data.entries()) {
    if (k === "rationale") continue;
    if (v === "" || v === null) continue;
    args[k] = v;
  }
  try {
    await postCommand("/control/command",
                       { kind, args, rationale });
    // Clear non-rationale inputs so the next action starts clean,
    // but leave rationale (operator often refines it across actions).
    form.querySelectorAll("input").forEach((i) => {
      if (i.name !== "rationale") i.value = i.defaultValue;
    });
    await refreshManualControl();
  } catch (e) {
    alert(`${kind} failed: ` + (e?.message || e));
  }
}

async function quickAction(kind) {
  const rationale = window.prompt(
    `Reason for ${KIND_LABEL[kind] || kind}?\n` +
    `(required — recorded in the audit log)`
  );
  if (rationale === null) return;
  const trimmed = (rationale || "").trim();
  if (!trimmed) {
    alert("A rationale is required.");
    return;
  }
  try {
    await postCommand("/control/command",
                       { kind, args: {}, rationale: trimmed });
    await refreshManualControl();
  } catch (e) {
    alert(`${kind} failed: ` + (e?.message || e));
  }
}

export function initManualControl(api) {
  _api = api;

  // Topbar Take Control button — toggles based on current state. The
  // banner has its own Release button as a more obvious off-ramp.
  const tc = document.getElementById("take-control");
  if (tc) {
    tc.addEventListener("click", async () => {
      if (tc.classList.contains("active")) await releaseControl();
      else                                   await takeControl();
    });
  }
  const releaseBtn = document.getElementById("release-control-banner");
  if (releaseBtn) {
    releaseBtn.addEventListener("click", () => releaseControl());
  }

  // Hardware Controls panel — wire each form + every quick-action button.
  const panel = document.getElementById("hardware-controls");
  if (panel) {
    panel.querySelectorAll("form.hw-form").forEach((form) => {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        submitFormAction(form);
      });
    });
    panel.querySelectorAll("button.hw-quick").forEach((btn) => {
      btn.addEventListener("click", () => quickAction(btn.dataset.kind));
    });
  }

  // Initial draw + poll loop.
  refreshManualControl();
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(refreshManualControl, POLL_MS);
}
