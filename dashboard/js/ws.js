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
