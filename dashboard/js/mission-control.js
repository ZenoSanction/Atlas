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
      const at = v && v.decided_at ? `decided ${fmtClock(v.decided_at)}` : "";
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
    // Memory count (live from /api/mission-control)
    const memCount = lane.querySelector('[data-field="memory_count"]');
    if (memCount) memCount.textContent = status.memory_count ?? 0;

    // Inbox — sticky display of recent inbound relays. Pulse when a
    // new item arrives so the user actually notices the ping.
    const inboxCount = lane.querySelector('[data-field="inbox_count"]');
    const inboxItems = lane.querySelector('[data-field="inbox_items"]');
    const inboxPanel = lane.querySelector('[data-field="inbox-panel"]');
    const inbox = status.inbox || [];
    if (inboxCount) inboxCount.textContent = inbox.length;
    if (inboxItems) {
      if (!inbox.length) {
        inboxItems.innerHTML = '<div class="inbox-empty">no inbound relays yet</div>';
      } else {
        inboxItems.innerHTML = inbox.slice(0, 4).map(m => `
          <div class="inbox-item">
            <span class="inbox-from">${esc(m.sender)}</span>
            <span class="inbox-kind">[${esc(m.kind)}]</span>
            <span class="inbox-summary">${esc(m.summary || "(no summary)")}</span>
            <span class="inbox-ts">${fmtClock(m.at)}</span>
          </div>`).join("");
      }
    }
    // Pulse animation when last_inbox_at changes vs what we showed last
    if (inboxPanel && status.last_inbox_at) {
      const prev = inboxPanel.dataset.lastAt;
      if (prev && prev !== status.last_inbox_at) {
        inboxPanel.classList.remove("ping");
        // force reflow so the animation restarts even if class was just removed
        void inboxPanel.offsetWidth;
        inboxPanel.classList.add("ping");
      }
      inboxPanel.dataset.lastAt = status.last_inbox_at;
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
  // Render UTC timestamps as HH:MM:SS in Eastern (auto EST/EDT)
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false,
    });
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

  // Memory: lazy-load on first expand, add via form, delete inline
  document.querySelectorAll(".agent-lane").forEach((lane) => {
    const agent = lane.dataset.agent;
    const details = lane.querySelector(".agent-memories");
    if (!details || details.dataset.bound) return;
    details.dataset.bound = "1";
    details.addEventListener("toggle", () => {
      if (details.open) loadMemories(api, agent, lane);
    });
    const form = lane.querySelector(".memory-add");
    if (form) {
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const input = form.querySelector("input[type=text]");
        const pinned = form.querySelector('input[name=pinned]').checked;
        const content = (input.value || "").trim();
        if (!content) return;
        try {
          await api(`/agents/${agent}/memory`, {
            method: "POST",
            body: JSON.stringify({ content, pinned }),
          });
          input.value = "";
          form.querySelector('input[name=pinned]').checked = false;
          await loadMemories(api, agent, lane);
        } catch (e) {
          alert("Failed to add memory: " + e.message);
        }
      });
    }
    // Delegate clicks on delete buttons inside the list
    const list = lane.querySelector(".memory-list");
    if (list) {
      list.addEventListener("click", async (ev) => {
        const btn = ev.target.closest("button[data-action]");
        if (!btn) return;
        const id = btn.dataset.id;
        const action = btn.dataset.action;
        if (action === "delete") {
          if (!confirm("Forget this memory?")) return;
          await api(`/agents/${agent}/memory/${id}`, { method: "DELETE" });
        } else if (action === "pin" || action === "unpin") {
          await api(`/agents/${agent}/memory/${id}`, {
            method: "PATCH",
            body: JSON.stringify({ pinned: action === "pin" }),
          });
        }
        await loadMemories(api, agent, lane);
      });
    }
  });
}

async function loadMemories(api, agent, lane) {
  const list = lane.querySelector(".memory-list");
  if (!list) return;
  try {
    const r = await api(`/agents/${agent}/memory?limit=50`);
    if (!r.memories.length) {
      list.innerHTML = `<em class="muted">no memories yet — type below to teach this agent something</em>`;
      return;
    }
    list.innerHTML = r.memories.map(m => `
      <div class="memory-item ${m.pinned ? "pinned" : ""} ${m.agent === "shared" ? "shared" : ""}">
        <div class="memory-meta">
          <span class="ts">${fmtClock(m.created_at)}</span>
          ${m.agent === "shared" ? '<span class="badge shared">shared</span>' : ""}
          ${m.pinned ? '<span class="badge pinned">📌 pinned</span>' : ""}
        </div>
        <div class="memory-content">${esc(m.content)}</div>
        <div class="memory-actions">
          <button class="btn-link" data-action="${m.pinned ? "unpin" : "pin"}" data-id="${m.id}">${m.pinned ? "unpin" : "pin"}</button>
          <button class="btn-link" data-action="delete" data-id="${m.id}">forget</button>
        </div>
      </div>
    `).join("");
  } catch (e) {
    list.innerHTML = `<em class="muted">error: ${esc(e.message)}</em>`;
  }
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
