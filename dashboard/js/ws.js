// WebSocket connection with auto-reconnect and event-stream rendering.

let socket = null;
let reconnectDelay = 1000;
const MAX_EVENTS = 200;

export function connectEvents() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/events`;
  socket = new WebSocket(url);

  socket.onopen = () => {
    reconnectDelay = 1000;
    document.getElementById("ws-indicator").classList.add("connected");
  };
  socket.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      handleEvent(data);
    } catch (e) {
      console.error("WS parse:", e);
    }
  };
  socket.onclose = () => {
    document.getElementById("ws-indicator").classList.remove("connected");
    setTimeout(connectEvents, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5, 15000);
  };
  socket.onerror = (e) => console.warn("WS error:", e);
}

function handleEvent(data) {
  if (data.type === "connected") return;
  // Bus-initiated close: server says we've fallen too far behind.
  // ws.js will be reconnected by the onclose handler.
  if (data && data.__bus_close__) {
    console.warn("WebSocket closed by server:", data.reason);
    return;
  }
  // Protection sequence events: shutdown/startup steps. Renders a
  // small live panel on the Tonight tab and also gets logged in the
  // standard event stream below.
  if (data.type === "protection") {
    handleProtectionEvent(data);
  }
  const events = document.getElementById("events");
  if (!events) return;

  const el = document.createElement("div");
  el.className = "event";
  if (data.type === "emergency") el.classList.add("emergency");
  if (data.sender) el.classList.add(`from-${data.sender}`);

  const ts = data.sent_at ? new Date(data.sent_at) : new Date();
  const tsStr = ts.toLocaleTimeString();

  if (data.type === "emergency") {
    el.innerHTML = `<span class="ts">${tsStr}</span><span class="who">EMERGENCY</span>${esc(data.message || data.code)}`;
  } else if (data.type === "verdict") {
    el.classList.add("kind-verdict");
    const prev = data.previous ? ` (was ${esc(data.previous)})` : "";
    el.innerHTML = `
      <span class="ts">${tsStr}</span>
      <span class="who">OPERATOR</span>
      <span class="kind">[verdict]</span>
      ${esc(data.verdict)}${prev} — ${esc(data.reason || "")}
    `;
  } else if (data.type === "assessment") {
    el.classList.add("kind-assessment");
    el.innerHTML = `
      <span class="ts">${tsStr}</span>
      <span class="who">CRITIC</span>
      <span class="kind">[${esc(data.severity || "ok")}]</span>
      ${esc(data.summary || "")}
    `;
  } else if (data.type === "plan_update") {
    el.classList.add("kind-plan");
    el.innerHTML = `
      <span class="ts">${tsStr}</span>
      <span class="who">PLANNER</span>
      <span class="kind">[${esc(data.kind || "plan")}]</span>
      ${data.visible} visible / ${data.active_campaigns} active campaigns (${esc(data.reason || "")})
    `;
  } else if (data.type === "session_archived") {
    el.classList.add("kind-archivist");
    el.innerHTML = `
      <span class="ts">${tsStr}</span>
      <span class="who">ARCHIVIST</span>
      <span class="kind">[session_archived]</span>
      ${esc(data.summary || "")}
    `;
  } else if (data.type === "archivist_tick") {
    el.classList.add("kind-archivist");
    el.innerHTML = `
      <span class="ts">${tsStr}</span>
      <span class="who">ARCHIVIST</span>
      <span class="kind">[idle]</span>
      ${esc(data.summary || "")}
    `;
  } else if (data.type === "research_pass") {
    el.classList.add("kind-oracle");
    el.innerHTML = `
      <span class="ts">${tsStr}</span>
      <span class="who">ORACLE</span>
      <span class="kind">[${esc(data.kind || "research")}]</span>
      ${esc(data.summary || "")}
    `;
  } else {
    el.innerHTML = `
      <span class="ts">${tsStr}</span>
      <span class="who">${esc(data.sender || "system")} → ${esc(data.recipient || "")}</span>
      <span class="kind">[${esc(data.kind || "")}]</span>
    `;
  }
  events.prepend(el);
  while (events.children.length > MAX_EVENTS) {
    events.removeChild(events.lastChild);
  }
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---- Protection-sequence panel --------------------------------------------
// SafeShutdownSequence / SafeStartupSequence emit per-step progress events
// through the bus. We accumulate them into a tiny in-memory ring per
// direction and render onto #protection-panel. Operator sees live
// "stop_guiding ✓ 1.4s | park_mount ⟳ 8s | warm_camera ⋯" while a
// safe-shutdown is in progress.
const _protSequences = { shutdown: null, startup: null };
const _AUTO_HIDE_MS = 5 * 60 * 1000;   // hide after 5 min of inactivity

function handleProtectionEvent(data) {
  const dir = data.direction || "shutdown";
  let seq = _protSequences[dir];
  const phase = data.phase || "";
  // New sequence — clear prior steps for this direction.
  if (phase.endsWith("_started")) {
    seq = _protSequences[dir] = {
      direction: dir, started_at: data.sent_at || new Date().toISOString(),
      reason: data.reason || "", steps: [], state: "running",
      completed_at: null,
    };
  }
  if (!seq) {
    // step landed without a started_at marker (cold load) — synthesize
    seq = _protSequences[dir] = {
      direction: dir, started_at: data.sent_at || new Date().toISOString(),
      reason: data.reason || "", steps: [], state: "running",
      completed_at: null,
    };
  }
  if (phase === "step") {
    seq.steps.push({
      name: data.step, state: data.state,
      elapsed_s: data.elapsed_s, detail: data.detail || "",
      summary: data.summary || "",
      at: data.sent_at,
    });
  }
  if (phase.endsWith("_complete")) {
    seq.state = data.state || "done";
    seq.completed_at = data.sent_at || new Date().toISOString();
    seq.summary = data.summary || "";
    // Schedule auto-hide for completed sequences
    setTimeout(() => {
      if (_protSequences[dir] && _protSequences[dir].completed_at) {
        _protSequences[dir] = null;
        renderProtectionPanel();
      }
    }, _AUTO_HIDE_MS);
  }
  renderProtectionPanel();
}

function renderProtectionPanel() {
  const panel = document.getElementById("protection-panel");
  if (!panel) return;
  const active = [_protSequences.shutdown, _protSequences.startup]
                    .filter(Boolean);
  if (active.length === 0) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  panel.classList.remove("hidden");
  panel.innerHTML = active.map(seq => {
    const dirCls = seq.direction === "shutdown" ? "crit" : "ok";
    const titleTxt = seq.direction === "shutdown"
      ? "SAFE SHUTDOWN" : "SAFE STARTUP";
    const stateLabel = seq.state === "running" ? "in progress…"
                       : seq.state === "safe" ? "complete — safed"
                       : seq.state === "ready" ? "complete — ready"
                       : seq.state === "needs_attention" ? "complete — needs attention"
                       : seq.state;
    const reasonTxt = seq.reason
      ? `<span class="muted">${esc(seq.reason)}</span>` : "";
    const stepRows = seq.steps.map(st => {
      const icon = ({"ok": "✓", "skipped": "—", "timeout": "⚠", "error": "✗"})[st.state] || "⟳";
      const cls = ({"ok":"ok","skipped":"muted","timeout":"warn","error":"crit"})[st.state] || "";
      const tail = st.detail ? ` <span class="muted">${esc(st.detail)}</span>` : "";
      return `<div class="prot-step ${cls}">
        <span class="prot-icon">${icon}</span>
        <span class="prot-name">${esc(st.name)}</span>
        <span class="prot-elapsed muted">${st.elapsed_s != null ? st.elapsed_s + "s" : ""}</span>
        ${tail}
      </div>`;
    }).join("");
    return `<div class="prot-card prot-${seq.direction}">
      <header>
        <span class="plan-state ${dirCls}">${titleTxt}</span>
        <span>${esc(stateLabel)}</span>
        ${reasonTxt}
      </header>
      <div class="prot-steps">${stepRows}</div>
    </div>`;
  }).join("");
}
