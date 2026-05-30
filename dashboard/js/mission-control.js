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

// Shared TTS module — chunks long replies into utterance-sized pieces
// and runs a pause/resume keep-alive pump. Previously this file's local
// speakReply sliced text to 500 chars AND hit Chrome's ~15s
// single-utterance cutoff, so any ATLAS reply over ~10 sentences went
// silent partway through.
import { speak as ttsSpeak } from "/static/js/tts.js";
// Voice-input secure-context check (LAN HTTP blocks mic in Chrome).
import { applyVoiceSupportToButton } from "/static/js/voice-support.js";

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

  // GO/NO-GO/WAITING banner — driven by the comprehensive preflight if
  // it has run, else falls back to the legacy weather-only verdict.
  const banner = document.getElementById("gonogo");
  const txt = document.getElementById("gonogo-text");
  const meta = document.getElementById("gonogo-meta");
  if (banner && txt) {
    banner.classList.remove("ok", "warn", "crit", "neutral", "waiting");
    const pf = mc.preflight;
    const v = mc.verdict;
    let label = "UNKNOWN", reason = "", at = "";
    if (pf && pf.verdict) {
      label = pf.verdict; reason = pf.reason || ""; at = pf.assessed_at;
    } else if (v) {
      label = v.verdict; reason = v.reason || ""; at = v.decided_at;
    }
    if (label === "GO") banner.classList.add("ok");
    else if (label === "CAUTION") banner.classList.add("warn");
    else if (label === "NO-GO") banner.classList.add("crit");
    else if (label === "WAITING") banner.classList.add("waiting");
    else banner.classList.add("neutral");
    txt.textContent = `${label} — ${reason || (label === "UNKNOWN" ? "starting up" : "")}`;
    if (meta) {
      const obs = mc.observatory_name ? `${mc.observatory_name} · ` : "";
      const sim = mc.simulation_mode ? "SIMULATION MODE · " : "";
      const t = at ? `decided ${fmtClock(at)}` : "";
      meta.textContent = obs + sim + t;
    }
  }

  // Session Readiness panel — per-gate breakdown
  const gatesEl = document.getElementById("readiness-gates");
  const nextEl = document.getElementById("readiness-next-action");
  if (gatesEl) {
    const pf = mc.preflight;
    if (!pf || !pf.gates || !pf.gates.length) {
      gatesEl.innerHTML = `<div class="empty">Pre-flight running — first cycle within 2 min.</div>`;
      if (nextEl) nextEl.textContent = "";
    } else {
      gatesEl.innerHTML = pf.gates.map(g => `
        <div class="gate gate-${g.status}">
          <span class="gate-icon">${gateIcon(g.status)}</span>
          <span class="gate-label">${esc(g.label)}</span>
          <span class="gate-message">${esc(g.message)}</span>
        </div>
      `).join("");
      if (nextEl) nextEl.textContent = pf.next_action || "";
    }
  }

  // Cache the verdict + weather-monitor globally so renderWorkflow()
  // can fold the execution-gate badge and the monitoring-mode pill
  // into the plan panel header.
  window._lastVerdict = mc.verdict || null;
  window._lastWeatherMonitor = mc.weather_monitor || null;
  renderWorkflow(mc.session_review);

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

  // Message flow — sender → recipient [kind] + an abbreviated preview
  // of the payload so the operator sees what each message actually
  // says, not just that something was sent.
  const flow = document.getElementById("message-flow");
  if (flow) {
    const items = mc.message_flow || [];
    flow.innerHTML = items.length
      ? items.map(m => {
          const lc = m.lifecycle || {};
          const status = (lc.status || "sent").toLowerCase();
          const lcCls = {
            delivered:  "lc-delivered",
            processing: "lc-processing",
            done:       "lc-done",
            failed:     "lc-failed",
          }[status] || "lc-unknown";
          const lcLabel = {
            delivered:  "delivered",
            processing: "processing…",
            done:       "done",
            failed:     "failed",
          }[status] || status;
          const errTip = lc.error ? ` title="${esc(lc.error)}"` : "";
          return `
          <div class="flow-item">
            <span class="ts">${fmtClock(m.sent_at)}</span>
            <span class="who">${esc(m.sender || "system")}</span>
            →
            <span class="who">${esc(m.recipient || "—")}</span>
            <span class="kind">[${esc(m.kind || "")}]</span>
            <span class="lc-pill ${lcCls}"${errTip}>${esc(lcLabel)}</span>
            <span class="flow-snippet">${esc(payloadSnippet(m.payload))}</span>
          </div>`;
        }).join("")
      : '<div class="empty">no messages yet</div>';
  }
}

// Abbreviated payload preview for the message-flow rows. Picks the
// most informative field available, then truncates. Order of preference:
//
//   summary           — most agents already write a one-line summary
//   reason            — verdict changes, blocks
//   message           — alerts
//   target_name       — candidate-target hand-offs
//   kind              — explicit sub-kind ("plan_advisory_request" etc.)
//   first 60 chars of JSON for everything else
//
// Cap length so the row stays single-line; a fuller view can come from
// the AgentMessage DB row later.
function payloadSnippet(payload) {
  if (!payload || typeof payload !== "object") return "";
  const pick = (...keys) => {
    for (const k of keys) {
      const v = payload[k];
      if (v !== undefined && v !== null && String(v).trim() !== "") return String(v);
    }
    return "";
  };
  let s = pick("summary", "reason", "message", "target_name");
  if (!s) {
    const subKind = pick("kind");
    if (subKind) {
      // Combine sub-kind with the next-most-informative field if present
      const extra = pick("verdict", "session_id", "advisory_count",
                            "frames_done", "filter");
      s = extra ? `${subKind}: ${extra}` : subKind;
    }
  }
  if (!s) {
    try {
      s = JSON.stringify(payload).slice(0, 80);
    } catch {
      s = "";
    }
  }
  // Truncate single-line preview
  s = s.replace(/\s+/g, " ").trim();
  return s.length > 120 ? s.slice(0, 117) + "…" : s;
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
  // Render UTC timestamps as h:mm:ss AM/PM in Eastern (auto EST/EDT).
  // Defensive: if the ISO string is missing a trailing Z / offset, JS
  // treats it as local time. Append Z so the parse anchors to UTC.
  if (!iso) return "";
  try {
    const safe = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + "Z";
    return new Date(safe).toLocaleTimeString("en-US", {
      timeZone: "America/New_York",
      hour: "numeric", minute: "2-digit", second: "2-digit",
      hour12: true,
    });
  } catch { return iso; }
}

function wireChatForms(api) {
  document.querySelectorAll(".agent-chat").forEach((form) => {
    if (form.dataset.bound) return;
    form.dataset.bound = "1";
    const agent = form.dataset.agent;
    const input = form.querySelector("input");
    const micBtn = form.querySelector(".lane-mic");
    const history = document.querySelector(`.agent-chat-history[data-agent="${agent}"]`);

    // Voice input — Web Speech API. Same wiring as the ATLAS-tab chat,
    // per-lane so each agent's chat input gets its own mic button.
    // Boot-time check disables the button with an explanatory tooltip
    // when the origin is plain HTTP from a non-localhost address
    // (warm-room PC over LAN), so the user sees "switch to HTTPS"
    // up front instead of a silent click-and-nothing-happens.
    if (micBtn) {
      const verdict = applyVoiceSupportToButton(micBtn);
      if (verdict.usable) {
        const Recog = window.SpeechRecognition || window.webkitSpeechRecognition;
        const recog = new Recog();
        recog.continuous = false;
        recog.interimResults = false;
        recog.lang = "en-US";
        micBtn.addEventListener("click", () => {
          if (micBtn.classList.contains("recording")) {
            recog.stop();
            micBtn.classList.remove("recording");
          } else {
            micBtn.classList.add("recording");
            recog.start();
          }
        });
        recog.onresult = (ev) => {
          const txt = ev.results[0][0].transcript;
          input.value = txt;
          micBtn.classList.remove("recording");
          form.dispatchEvent(new Event("submit"));
        };
        recog.onend = () => micBtn.classList.remove("recording");
        recog.onerror = () => micBtn.classList.remove("recording");
      }
      // When !verdict.usable, applyVoiceSupportToButton already set the
      // disabled state + tooltip, so we have nothing else to do here.
    }

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
        // Speak the reply if TTS available + lane has TTS enabled
        speakReply(r.reply || "");
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

// Browser TTS — delegates to the shared tts.js module. The module
// chunks on sentence boundaries (Chrome cuts off any single utterance
// past ~200 chars / ~15s of audio) and runs a pause/resume keep-alive
// pump so multi-paragraph replies play through to the end. Honours
// the same localStorage key the topbar 🔊 toggle writes to.
function speakReply(text) {
  ttsSpeak(text);
}

function gateIcon(status) {
  switch (status) {
    case "ok":       return "✓";
    case "warning":  return "⚠";
    case "critical": return "✗";
    case "missing":  return "○";
    case "unknown":  return "?";
    default:         return "·";
  }
}

// Session Plan + Advisories. The plan is READY the moment the Planner
// builds it; advisories are informational notes about what's happening
// around the plan (weather, moon, revisits). They never change the
// plan's state. Whether to *execute* a plan is a separate question —
// handled by the OperatorVerdict (the GO/CAUTION/NO-GO banner at the
// top of the Tonight tab). Storm rolls in → verdict goes NO-GO. Plan
// stays READY. Storm passes → verdict back to GO. Same plan, no rebuild.
function renderWorkflow(review) {
  const stages = document.getElementById("workflow-stages");
  const idEl = document.getElementById("workflow-review-id");
  const body = document.getElementById("workflow-detail-body");
  if (!stages) return;

  if (!review || !review.state) {
    stages.innerHTML = `<div class="empty">No plan yet — Planner builds one on startup and every 30 min.</div>`;
    if (idEl) idEl.textContent = "";
    if (body) body.innerHTML = "";
    return;
  }

  if (idEl) {
    const startedClock = fmtClock(review.started_at);
    idEl.textContent = `id ${review.review_id} · started ${startedClock}`;
  }

  const counts = review.advisory_counts || { info: 0, warning: 0, critical: 0 };
  const stateBadge = ({
    ready:      `<span class="plan-state ok">PLAN READY</span>`,
    building:   `<span class="plan-state warn">building…</span>`,
    replanned:  `<span class="plan-state">replanned</span>`,
  })[review.state] || `<span class="plan-state">${esc(review.state)}</span>`;

  // Execution gate: the verdict from /api/mission-control. The Plan
  // panel surfaces it inline so the operator sees plan + execution
  // status together. Note the deliberate phrasing: the plan stays
  // READY even when execution is blocked — they're decoupled.
  const v = window._lastVerdict || null;
  let execLine = "";
  if (v) {
    const cls = ({"GO":"ok","CAUTION":"warn","NO-GO":"crit"})[v.verdict] || "";
    execLine = `<div class="exec-state">
      Execution: <span class="plan-state ${cls}">${esc(v.verdict)}</span>
      <span class="muted">${esc(v.reason || "")}</span>
    </div>`;
  }
  // Monitoring mode: idle / active / borderline + age of the cached
  // weather snapshot. Operator sees how tight the watch is.
  const wm = window._lastWeatherMonitor || null;
  let monitorLine = "";
  if (wm && wm.mode) {
    const modeCls = wm.mode === "borderline" ? "warn"
                    : wm.mode === "active" ? "ok" : "";
    const ageTxt = wm.age_seconds != null
      ? `${Math.round(wm.age_seconds)}s ago`
      : "no data yet";
    const ttlTxt = wm.ttl_s ? `(refresh ≤ ${wm.ttl_s}s)` : "";
    monitorLine = `<div class="exec-state monitor-state">
      Weather monitor:
      <span class="plan-state ${modeCls}">${esc(wm.mode.toUpperCase())}</span>
      <span class="muted">snapshot ${ageTxt} ${ttlTxt}</span>
    </div>`;
  }

  // Window the plan covers + current day phase + countdown. Read
  // straight from the plan dict — both fields are populated by the
  // clock-aware _rebuild_plan path.
  const plan = review.plan || {};
  const win = plan.window || null;
  const dp = plan.day_phase || null;
  let windowLine = "";
  if (win && win.dusk_utc && win.dawn_utc) {
    const isInProgress = !!win.is_imaging_window_now;
    const start = win.effective_start_utc || win.dusk_utc;
    const remHrs = win.remaining_hours != null
      ? `${win.remaining_hours}h remaining`
      : `${win.hours}h total`;
    const label = isInProgress
      ? `Tonight's dark window (in progress):`
      : `Tonight's dark window:`;
    windowLine = `<div class="exec-state">
      ${label}
      <span class="plan-state">${fmtClock(start)} &rarr; ${fmtClock(win.dawn_utc)}</span>
      <span class="muted">${remHrs}</span>
    </div>`;
  }
  let phaseLine = "";
  if (dp && dp.phase) {
    const phaseClean = dp.phase.replace(/_/g, " ").toUpperCase();
    const phaseCls = dp.is_imaging_window ? "ok"
                    : dp.phase === "day" ? "" : "warn";
    const nextLabel = dp.next_phase
      ? dp.next_phase.replace(/_/g, " ")
      : "next phase";
    const inMin = dp.minutes_until_next_phase != null
      ? `${Math.round(dp.minutes_until_next_phase)} min`
      : "?";
    phaseLine = `<div class="exec-state">
      Day phase:
      <span class="plan-state ${phaseCls}">${esc(phaseClean)}</span>
      <span class="muted">sun ${dp.sun_altitude_deg}&deg; · ${esc(nextLabel)} in ${inMin}</span>
    </div>`;
  }

  stages.innerHTML = `
    <div class="plan-summary">
      ${stateBadge}
      <div class="adv-counts">
        ${counts.critical ? `<span class="pill crit">${counts.critical} critical</span>` : ""}
        ${counts.warning  ? `<span class="pill warn">${counts.warning} warning</span>` : ""}
        ${counts.info     ? `<span class="pill">${counts.info} info</span>` : ""}
        ${(!counts.critical && !counts.warning && !counts.info)
            ? `<span class="muted">no advisories</span>` : ""}
      </div>
    </div>
    ${windowLine}
    ${phaseLine}
    ${execLine}
    ${monitorLine}
  `;

  if (!body) return;
  const advisories = review.advisories || [];
  if (advisories.length === 0) {
    body.innerHTML = `<div class="empty">no advisories filed against this plan</div>`;
    return;
  }
  // Group by source so the operator sees "Critic said:" / "Oracle said:"
  const bySource = {};
  for (const a of advisories) {
    (bySource[a.source] = bySource[a.source] || []).push(a);
  }
  body.innerHTML = Object.entries(bySource).map(([source, items]) => `
    <div class="advisory-group">
      <h4>${esc(source)} <span class="muted">(${items.length})</span></h4>
      ${items.map(a => `
        <div class="advisory advisory-${a.severity}">
          <span class="adv-kind">${esc(a.kind)}</span>
          <span class="adv-sev">[${esc(a.severity)}]</span>
          ${esc(a.message)}
          ${a.target_name ? `<span class="muted">— ${esc(a.target_name)}</span>` : ""}
        </div>
      `).join("")}
    </div>
  `).join("");
}
