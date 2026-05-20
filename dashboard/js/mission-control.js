// Mission Control — the redesigned Tonight tab.
//
// Renders 5 agent lanes side by side, each showing the agent's current task,
// next-tick countdown, recent decisions, and a per-agent chat input. The
// message-flow column shows live inter-agent messages.
//
// Refresh model:
//   - HTTP poll every 3 seconds for /api/mission-control (cheap; in-memory state)
//   - WebSocket pushes new events into the message-flow + animates the
//     relevant lane when their task changes

let _timer = null;
let _historyByAgent = {};   // agent name -> array of {who, text}

export function renderMissionControl(api) {
  refreshMissionControl(api);
  if (_timer === null) {
    _timer = setInterval(() => refreshMissionControl(api), 3000);
  }
  wireChatForms(api);
}

export async function refreshMissionControl(api) {
  let mc;
  try {
    mc = await api("/mission-control");
  } catch (e) {
    return;
  }

  // GO/NO-GO banner
  const banner = document.getElementById("gonogo");
  const txt = document.getElementById("gonogo-text");
  const meta = document.getElementById("gonogo-meta");
  if (banner && txt) {
    banner.classList.remove("ok", "warn", "crit", "neutral");
    const v = mc.verdict;
    if (v && v.verdict === "GO") banner.classList.add("ok");
    else if (v && v.verdict === "CAUTION") banner.classList.add("warn");
    else if (v && v.verdict === "NO-GO") banner.classList.add("crit");
    else banner.classList.add("neutral");
    txt.textContent = v ? `${v.verdict} — ${v.reason || ""}` : "Awaiting first Critic assessment…";
    if (meta) {
      const obs = mc.observatory_name ? `${mc.observatory_name} · ` : "";
      const sim = mc.simulation_mode ? "SIMULATION MODE · " : "";
      const at = v && v.decided_at ? `decided ${v.decided_at}` : "";
      meta.textContent = obs + sim + at;
    }
  }

  // Per-agent lanes
  for (const [name, status] of Object.entries(mc.agents || {})) {
    const lane = document.querySelector(`.agent-lane[data-agent="${name}"]`);
    if (!lane) continue;
    setField(lane, "current_task", status.current_task || "—");
    const stateEl = lane.querySelector('[data-field="state"]');
    if (stateEl) {
      const s = status.safe_mode ? "safe-mode"
                : !status.running ? "stopped"
                : (status.state || "idle");
      stateEl.textContent = `● ${s}`;
      stateEl.className = "agent-state state-" + s.replace(/-/g, "");
    }
    setField(lane, "last_decision",
              status.last_decision ? `last: ${status.last_decision}` : "");
    setField(lane, "next_tick", fmtNextTick(status));
    const ul = lane.querySelector('[data-field="recent_decisions"]');
    if (ul) {
      const items = (status.recent_decisions || []).slice(0, 6);
      ul.innerHTML = items.length
        ? items.map(d => `<li><span class="ts">${fmtClock(d.at)}</span> ${esc(d.decision_type)}${d.rationale ? ' — <span class="rationale">' + esc(d.rationale) + "</span>" : ""}</li>`).join("")
        : '<li class="empty">no decisions yet</li>';
    }
  }

  // Message flow
  const flow = document.getElementById("message-flow");
  if (flow) {
    const items = mc.message_flow || [];
    flow.innerHTML = items.length
      ? items.map(m => `
          <div class="flow-item">
            <span class="ts">${fmtClock(m.sent_at)}</span>
            <span class="who">${esc(m.sender || "system")}</span>
            →
            <span class="who">${esc(m.recipient || "—")}</span>
            <span class="kind">[${esc(m.kind || "")}]</span>
          </div>`).join("")
      : '<div class="empty">no messages yet</div>';
  }
}

function setField(lane, field, value) {
  const el = lane.querySelector(`[data-field="${field}"]`);
  if (el && el.textContent !== value) el.textContent = value;
}

function fmtNextTick(status) {
  if (!status.next_tick_at) return "";
  const target = new Date(status.next_tick_at).getTime();
  const remaining_s = Math.max(0, Math.floor((target - Date.now()) / 1000));
  const m = Math.floor(remaining_s / 60), s = remaining_s % 60;
  const kind = status.next_tick_kind || "tick";
  return `next ${kind} in ${m ? m + "m " : ""}${s}s`;
}

function fmtClock(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  } catch { return iso; }
}

function wireChatForms(api) {
  document.querySelectorAll(".agent-chat").forEach((form) => {
    if (form.dataset.bound) return;
    form.dataset.bound = "1";
    const agent = form.dataset.agent;
    const input = form.querySelector("input");
    const history = document.querySelector(`.agent-chat-history[data-agent="${agent}"]`);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const text = (input.value || "").trim();
      if (!text) return;
      input.value = "";
      _historyByAgent[agent] = _historyByAgent[agent] || [];
      _historyByAgent[agent].push({ who: "You", text });
      renderChatHistory(history, _historyByAgent[agent]);
      _historyByAgent[agent].push({ who: agent, text: "…thinking…", pending: true });
      renderChatHistory(history, _historyByAgent[agent]);
      try {
        const r = await api(`/agents/${agent}/chat`, {
          method: "POST",
          body: JSON.stringify({ message: text }),
        });
        // Replace the pending placeholder
        _historyByAgent[agent].pop();
        _historyByAgent[agent].push({
          who: agent + (r.safe_mode ? " (safe)" : ""),
          text: r.reply || "(no reply)",
        });
      } catch (e) {
        _historyByAgent[agent].pop();
        _historyByAgent[agent].push({ who: agent, text: `error: ${e.message}` });
      }
      renderChatHistory(history, _historyByAgent[agent]);
    });
  });
}

function renderChatHistory(container, history) {
  if (!container) return;
  // Keep at most last 6 turns
  const trimmed = history.slice(-6);
  container.innerHTML = trimmed.map(m => `
    <div class="chat-bubble ${m.pending ? "pending" : ""} ${m.who === "You" ? "user" : "agent"}">
      <span class="chat-who">${esc(m.who)}</span>
      <span class="chat-text">${esc(m.text)}</span>
    </div>
  `).join("");
  container.scrollTop = container.scrollHeight;
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
