"""Shared in-memory state between Critic, Operator, and the HTTP layer.

The Critic periodically writes its latest weather assessment here. The
Operator reads that and writes back its verdict (GO / CAUTION / NO-GO).
API routes read both for the dashboard's Tonight + Weather tabs.

This is intentionally a tiny module — no DB persistence, no asyncio
primitives. The agents' message bus already covers the durable +
ordered case; this module just gives us a cheap, current-value cache
so a dashboard request doesn't have to wait for the next 5-minute
Critic tick to render something useful.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from threading import Lock
from typing import Any, Optional


# ---- Verdict levels ---------------------------------------------------------

VERDICT_GO = "GO"
VERDICT_CAUTION = "CAUTION"
VERDICT_NOGO = "NO-GO"
VERDICT_UNKNOWN = "UNKNOWN"


# ---- Assessment shape -------------------------------------------------------

@dataclass
class MetricCheck:
    """One per-metric check the Critic ran (wind, dew margin, cloud, ...)."""
    metric: str
    severity: str  # "ok" | "warning" | "critical"
    value: Optional[float]
    threshold: Optional[float]
    note: str


@dataclass
class WeatherAssessment:
    """The Critic's latest read on the sky. Fed to the Operator."""
    observed_at: str            # ISO timestamp from Open-Meteo
    assessed_at: str            # ISO timestamp when the Critic ran
    overall_severity: str       # "ok" | "warning" | "critical"
    summary: str                # one-line plain-English summary
    checks: list[MetricCheck] = field(default_factory=list)
    raw_current: dict = field(default_factory=dict)
    # Forward-looking: rough quality bucket for each of the next N hours
    # ("ok"/"warning"/"critical"), so the dashboard can shade the timeline.
    hourly_severity: list[dict] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


@dataclass
class OperatorVerdict:
    """Operator's call, derived from the Critic's assessment + any active
    alerts + session state. The Tonight tab banner reads this directly."""
    decided_at: str
    verdict: str                # GO | CAUTION | NO-GO | UNKNOWN
    reason: str                 # one-line plain-English
    sources: list[str] = field(default_factory=list)   # what fed the call

    def to_jsonable(self) -> dict:
        return asdict(self)


# ---- Manual control (operator override) -------------------------------------

@dataclass
class ManualControl:
    """Tracks whether the human operator has 'taken control' away from the
    autonomous Operator agent.

    When engaged=True, the Operator agent stops dispatching session
    decisions, alert auto-fixes, and oracle replans. The pre-flight gate
    still publishes status, the Critic still reports weather, the
    dashboard still polls — but the *autonomy* is paused. Direct hardware
    commands from the dashboard's Hardware Controls panel are the only
    way work happens until control is released.

    Every manual action is logged with the supplied rationale so the
    morning report can reconstruct who did what and why."""
    engaged: bool = False
    engaged_at: Optional[str] = None      # ISO timestamp when taken
    released_at: Optional[str] = None     # ISO timestamp when released
    reason: str = ""                      # operator's stated rationale
    engaged_by: str = "operator"          # who took control (future: multi-user)
    last_action: Optional[dict] = None    # most recent manual hardware action
    action_count: int = 0                 # how many manual actions this session

    def to_jsonable(self) -> dict:
        return asdict(self)


# ---- Singleton store --------------------------------------------------------

@dataclass
class AgentLiveStatus:
    """What an agent is doing *right now*. Updated by the agent each time
    it transitions to a new phase. Read by the dashboard for the Mission
    Control lanes."""
    name: str                            # "planner" | "critic" | ...
    current_task: str = "idle"
    state: str = "idle"                  # "idle" | "working" | "waiting" | "safe-mode"
    last_decision: str = ""              # decision_type of the most recent log
    next_tick_at: Optional[str] = None   # ISO timestamp when next loop fires
    next_tick_kind: Optional[str] = None # e.g. "fast_loop" / "standard_loop"
    updated_at: str = ""
    recent_decisions: list[dict] = field(default_factory=list)
    recent_messages: list[dict] = field(default_factory=list)
    # Inter-agent relay tracking — kept sticky so the dashboard can show
    # "📬 from planner: tonight plan ready" persistently in the lane,
    # not just for the half-second between task transitions.
    inbox: list[dict] = field(default_factory=list)
    outbox: list[dict] = field(default_factory=list)
    last_inbox_at: Optional[str] = None  # ISO timestamp of newest inbox item

    def to_jsonable(self) -> dict:
        return asdict(self)


# ---- Execution snapshot (the new substrate behind "Right Now") --------------

@dataclass
class ExecutionSnapshot:
    """What ATLAS is actually doing inside the active slot.

    This is the small new write-target the doctrine calls for. Every
    other field surfaced by ``get_right_now()`` is *read* from an
    existing state slot (tonight_plan, session_review, verdict,
    weather assessment, manual control). Only these execution-level
    details have nowhere else to live, so they get their own slot.

    Fields are all optional — agents fill them in as work progresses.
    All-None is a valid state (nothing executing right now).
    """
    active_slot: dict | None = None          # {target_name, workflow, start_utc, end_utc}
    active_action: str | None = None         # "capturing L frame 18/30"
    active_frame: dict | None = None         # {filter, exposure_s, index, count}
    slot_progress: dict | None = None        # {elapsed_min, scheduled_min, frames_done, frames_total}
    next_action: str | None = None           # "slew to NGC 7331 at 02:15"
    next_action_at: str | None = None        # ISO
    planned_session_end: str | None = None   # ISO
    blocked_reason: str | None = None        # if execution is paused, why
    updated_at: str = ""

    def to_jsonable(self) -> dict:
        return asdict(self)


@dataclass
class PendingDecision:
    """A material change ATLAS is deliberating about.

    Doctrine: most autonomous systems are black boxes — they just act.
    ATLAS narrates its deliberation so the human can intervene with
    better information, or let ATLAS finish its thinking. Pending
    decisions appear in Right Now and on the dashboard. The decision
    has a timeout; on expiry ATLAS picks ``default_action`` (which
    usually means: don't change anything, OR safe-NO-GO if a real
    risk has developed).
    """
    id: str
    kind: str                               # pause | resume | drop_slot | truncate | swap | insert | safe_shutdown
    narration: str                          # human-readable explanation
    started_at: str                         # ISO
    decide_by: str                          # ISO timeout
    default_action: str                     # what happens on timeout
    evidence: dict = field(default_factory=dict)
    confidence_layer: str = "rules"         # rules | history | llm | unresolved
    severity: str = "info"                  # info | warning | critical

    def to_jsonable(self) -> dict:
        return asdict(self)


class _ObservatoryState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._assessment: WeatherAssessment | None = None
        self._verdict: OperatorVerdict | None = None
        self._tonight_plan: dict | None = None
        self._archivist_last: dict | None = None
        self._oracle_last: dict | None = None
        # ---- Execution snapshot + pending decisions (Right Now substrate) ----
        # These are the only NEW write-targets the doctrine introduces.
        # Everything else in Right Now is aggregated from existing slots.
        self._execution: ExecutionSnapshot = ExecutionSnapshot()
        self._pending_decisions: dict[str, PendingDecision] = {}
        # Per-agent live status. Mission Control reads from here.
        self._agent_status: dict[str, AgentLiveStatus] = {
            n: AgentLiveStatus(name=n)
            for n in ("planner", "critic", "operator", "archivist", "oracle")
        }
        # Inter-agent message ring buffer for the live flow column
        self._message_flow: list[dict] = []
        self._max_messages = 80
        # Per-message lifecycle status (delivered → processing →
        # done/failed). Keyed by Message.id. Capped at ~200 entries
        # via FIFO eviction on insert. The dashboard renders this
        # as a pill next to each message-flow row so the operator
        # can see WHERE a message got stuck.
        self._message_status: dict[str, dict] = {}
        self._message_status_order: list[str] = []
        self._max_message_status = 200
        # Latest comprehensive session-readiness pre-flight assessment.
        # The Operator runs this every 2 min and publishes the result here;
        # the dashboard's Session Readiness panel + the API both read it.
        self._preflight: dict | None = None
        # Most recent SessionReview (dict form) — the deterministic
        # plan → critic → operator → oracle → operator → planner pipeline.
        # The dashboard's Session Workflow panel reads this.
        self._session_review: dict | None = None
        # Current review-chain phase. Tracks the Planner→Critic→Operator
        # →Oracle→Planner relay so the dashboard can show "Critic
        # reviewing…", "Oracle suggesting revisits…", etc. and the
        # human can see the plan is being actively worked on.
        # Values: "draft" | "critic" | "operator" | "oracle" |
        #         "finalizing" | "final" | "stalled"
        self._review_phase: str = "final"
        self._review_phase_review_id: str | None = None
        self._review_phase_updated_at: str | None = None
        # Hash of the most recently published plan's *material*
        # fields (target list + dark window + active campaigns). The
        # Planner checks this before kicking the review chain — if
        # the hash matches the previous publication, the new plan is
        # functionally identical and the chain is skipped to save
        # bus traffic + downstream agent work. Reset on every
        # explicit operator-driven rebuild so a forced refresh
        # always re-fires the chain.
        self._last_plan_hash: str | None = None
        # asyncio.Event used by the Operator's verdict watcher.
        # set_verdict() fires it; the watcher awaits on it with a
        # generous timeout fallback. Replaces the old 15-s polling
        # loop — ~5,500 fewer wake-ups per day with identical
        # responsiveness."""
        import asyncio as _asyncio
        try:
            self._verdict_event: _asyncio.Event | None = _asyncio.Event()
        except RuntimeError:
            # No running loop at import time — defer creation
            self._verdict_event = None
        # Human "take control" override. When engaged the Operator agent
        # halts autonomous dispatch; the dashboard's Hardware Controls
        # panel becomes the only way work happens.
        self._manual_control: ManualControl = ManualControl()
        # Ring buffer of recent manual hardware commands (for the audit
        # panel in the dashboard + morning report).
        self._manual_actions: list[dict] = []
        self._max_manual_actions = 40

    # Critic writes here ----------------------------------------------------
    def set_assessment(self, a: WeatherAssessment) -> None:
        with self._lock:
            self._assessment = a

    def get_assessment(self) -> WeatherAssessment | None:
        with self._lock:
            return self._assessment

    # Operator writes here --------------------------------------------------
    def set_verdict(self, v: OperatorVerdict) -> OperatorVerdict | None:
        """Returns the previous verdict (or None) so callers can detect
        a change and broadcast accordingly. Fires the verdict event so
        the Operator's verdict watcher wakes immediately instead of
        polling on a 15-s timer."""
        with self._lock:
            prev = self._verdict
            self._verdict = v
        # Wake any verdict-watcher coroutines without holding the lock.
        try:
            ev = self._verdict_event
            if ev is None:
                import asyncio as _asyncio
                self._verdict_event = ev = _asyncio.Event()
            ev.set()
        except Exception:
            pass
        return prev

    def get_verdict(self) -> OperatorVerdict | None:
        with self._lock:
            return self._verdict

    async def wait_verdict_change(self, timeout_s: float = 300.0) -> bool:
        """Block until set_verdict() fires the event or timeout elapses.

        Returns True if the event fired (verdict changed), False on
        timeout. Replaces the old 15-s polling cadence — the watcher
        can sleep indefinitely until a real verdict transition happens,
        and the 5-min fallback timeout catches the rare case where the
        event was missed (e.g. import-time race)."""
        import asyncio as _asyncio
        if self._verdict_event is None:
            try:
                self._verdict_event = _asyncio.Event()
            except Exception:
                # No running loop yet — fall back to short sleep
                await _asyncio.sleep(min(15.0, timeout_s))
                return False
        ev = self._verdict_event
        ev.clear()
        try:
            await _asyncio.wait_for(ev.wait(), timeout=timeout_s)
            return True
        except _asyncio.TimeoutError:
            return False

    # Plan-hash skip — Planner writes the hash of the last published
    # plan's material fields so the next rebuild can skip the chain
    # entirely if the plan didn't change.
    def set_last_plan_hash(self, h: str | None) -> None:
        with self._lock:
            self._last_plan_hash = h

    def get_last_plan_hash(self) -> str | None:
        with self._lock:
            return self._last_plan_hash

    # Planner writes here ---------------------------------------------------
    def set_tonight_plan(self, plan: dict) -> None:
        with self._lock:
            self._tonight_plan = plan

    def get_tonight_plan(self) -> dict | None:
        with self._lock:
            return self._tonight_plan

    # Archivist writes here -------------------------------------------------
    def set_archivist_last(self, info: dict) -> None:
        with self._lock:
            self._archivist_last = info

    def get_archivist_last(self) -> dict | None:
        with self._lock:
            return self._archivist_last

    # Oracle writes here ----------------------------------------------------
    def set_oracle_last(self, info: dict) -> None:
        with self._lock:
            self._oracle_last = info

    def get_oracle_last(self) -> dict | None:
        with self._lock:
            return self._oracle_last

    # Per-agent live status (Mission Control) -------------------------------
    def update_agent_status(self, agent: str, **fields) -> AgentLiveStatus:
        """Patch fields on the named agent's live status. Returns the updated
        snapshot. The dashboard reads these via /api/mission-control."""
        with self._lock:
            status = self._agent_status.get(agent)
            if status is None:
                status = AgentLiveStatus(name=agent)
                self._agent_status[agent] = status
            for k, v in fields.items():
                if hasattr(status, k):
                    setattr(status, k, v)
            status.updated_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            return status

    def push_agent_decision(self, agent: str, decision: dict, limit: int = 8) -> None:
        with self._lock:
            status = self._agent_status.setdefault(agent, AgentLiveStatus(name=agent))
            status.recent_decisions.insert(0, decision)
            status.recent_decisions = status.recent_decisions[:limit]
            status.last_decision = decision.get("decision_type", "")

    def push_agent_message(self, agent: str, message: dict, limit: int = 12) -> None:
        with self._lock:
            status = self._agent_status.setdefault(agent, AgentLiveStatus(name=agent))
            status.recent_messages.insert(0, message)
            status.recent_messages = status.recent_messages[:limit]

    def get_agent_status(self, agent: str) -> AgentLiveStatus | None:
        with self._lock:
            return self._agent_status.get(agent)

    def get_all_agent_status(self) -> dict[str, AgentLiveStatus]:
        with self._lock:
            return dict(self._agent_status)

    # Inter-agent message flow ----------------------------------------------
    def push_message_flow(self, message: dict) -> None:
        with self._lock:
            self._message_flow.insert(0, message)
            self._message_flow = self._message_flow[:self._max_messages]

    # Per-message lifecycle tracking ---------------------------------------
    def set_message_status(self, message_id: str, status: str,
                              **details) -> None:
        """Update a message's lifecycle status.

        Status values used by the bus + agents:
          delivered  — bus put it on the recipient's queue
          processing — recipient's recv() returned it; handler started
          done       — handler returned normally
          failed     — handler raised an exception (error= field carries reason)

        Cap-and-evict: when the dict grows past _max_message_status,
        drop the oldest entry to keep memory bounded."""
        if not message_id:
            return
        from datetime import datetime as _dt
        with self._lock:
            existing = self._message_status.get(message_id) or {}
            existing.update(details)
            existing["status"] = status
            existing["updated_at"] = _dt.utcnow().isoformat(timespec="seconds") + "Z"
            history = existing.get("history") or []
            history.append({
                "status": status,
                "at": existing["updated_at"],
            })
            existing["history"] = history[-10:]   # cap timeline depth
            self._message_status[message_id] = existing
            if message_id in self._message_status_order:
                self._message_status_order.remove(message_id)
            self._message_status_order.append(message_id)
            # Evict oldest
            while len(self._message_status_order) > self._max_message_status:
                oldest = self._message_status_order.pop(0)
                self._message_status.pop(oldest, None)

    def get_message_status(self, message_id: str) -> dict | None:
        with self._lock:
            return dict(self._message_status.get(message_id) or {}) or None

    def get_message_status_map(self, ids: list[str]) -> dict[str, dict]:
        """Bulk lookup — used by the message-flow API to enrich every
        flow row in one pass without N separate dict accesses."""
        out: dict[str, dict] = {}
        with self._lock:
            for mid in ids:
                v = self._message_status.get(mid)
                if v is not None:
                    out[mid] = dict(v)
        return out

    def get_message_flow(self, limit: int = 80) -> list[dict]:
        with self._lock:
            return list(self._message_flow[:limit])

    # Comprehensive session pre-flight ---------------------------------------
    def set_preflight(self, preflight: dict) -> None:
        with self._lock:
            self._preflight = preflight

    def get_preflight(self) -> dict | None:
        with self._lock:
            return self._preflight

    # Session-planning workflow (multi-phase pipeline) ----------------------
    def set_review_phase(self, phase: str, *, review_id: str | None = None) -> None:
        """Update the current review-chain phase. The dashboard polls
        this so the Plan tab can show 'Critic reviewing…' / 'Oracle
        suggesting…' etc. while the chain is in flight."""
        from datetime import datetime as _dt
        with self._lock:
            self._review_phase = phase
            if review_id is not None:
                self._review_phase_review_id = review_id
            self._review_phase_updated_at = _dt.utcnow().isoformat(timespec="seconds") + "Z"

    def get_review_phase(self) -> dict:
        with self._lock:
            return {
                "phase": self._review_phase,
                "review_id": self._review_phase_review_id,
                "updated_at": self._review_phase_updated_at,
            }

    def set_session_review(self, review: dict) -> None:
        with self._lock:
            self._session_review = review

    def get_session_review(self) -> dict | None:
        with self._lock:
            return self._session_review

    def append_advisories(self, review_id: str,
                            advisories: list[dict]) -> bool:
        """Atomically append advisories to the live session_review.

        Multiple agents (Critic, Oracle) file advisories concurrently
        against the same plan. Without atomic append they race —
        each agent reads the bare plan, adds its own, writes back,
        and the slower writer overwrites the faster one. This method
        holds the lock for the full read-modify-write so both
        agents' findings survive.

        Only modifies the live review if its review_id matches.
        Returns True if the append happened, False if the live
        review has rotated to a newer plan (in which case the
        caller's advisories are stale and discarded)."""
        with self._lock:
            current = self._session_review
            if current is None:
                return False
            if current.get("review_id") != review_id:
                return False
            adv_list = current.get("advisories") or []
            adv_list.extend(advisories)
            current["advisories"] = adv_list
            # Recompute the severity counts the dashboard reads.
            counts = {"info": 0, "warning": 0, "critical": 0}
            for a in adv_list:
                s = a.get("severity")
                if s in counts:
                    counts[s] += 1
            current["advisory_counts"] = counts
            # Add history entries for each new advisory so the
            # audit trail captures both agents' contributions.
            history = current.get("history") or []
            for a in advisories:
                history.append({
                    "kind": "advisory",
                    "at": a.get("at"),
                    "source": a.get("source"),
                    "severity": a.get("severity"),
                    "message": (a.get("message") or "")[:160],
                })
            current["history"] = history
            return True

    # Per-agent inbox + outbox (sticky relay visibility) --------------------
    def push_inbox(self, agent: str, item: dict, limit: int = 8) -> None:
        with self._lock:
            status = self._agent_status.setdefault(agent, AgentLiveStatus(name=agent))
            status.inbox.insert(0, item)
            status.inbox = status.inbox[:limit]
            status.last_inbox_at = item.get("at") or status.last_inbox_at

    def push_outbox(self, agent: str, item: dict, limit: int = 8) -> None:
        with self._lock:
            status = self._agent_status.setdefault(agent, AgentLiveStatus(name=agent))
            status.outbox.insert(0, item)
            status.outbox = status.outbox[:limit]

    # Manual control override -----------------------------------------------
    def set_manual_control(self, reason: str, by: str = "operator") -> ManualControl:
        """Engage human override. Operator agent will park its autonomy
        until clear_manual_control() runs. Returns the new snapshot."""
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with self._lock:
            self._manual_control = ManualControl(
                engaged=True,
                engaged_at=now,
                released_at=None,
                reason=(reason or "no reason given").strip()[:300],
                engaged_by=by,
                last_action=None,
                action_count=0,
            )
            return self._manual_control

    def clear_manual_control(self, reason: str = "") -> ManualControl:
        """Release human override and let the Operator resume autonomy.
        Returns the new (disengaged) snapshot."""
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with self._lock:
            prev = self._manual_control
            self._manual_control = ManualControl(
                engaged=False,
                engaged_at=prev.engaged_at,
                released_at=now,
                reason=(reason or "released").strip()[:300],
                engaged_by=prev.engaged_by,
                last_action=prev.last_action,
                action_count=prev.action_count,
            )
            return self._manual_control

    def get_manual_control(self) -> ManualControl:
        with self._lock:
            return self._manual_control

    def is_manual(self) -> bool:
        with self._lock:
            return self._manual_control.engaged

    def record_manual_action(self, action: dict) -> None:
        """Append a manual hardware command to the audit ring buffer and
        update the live snapshot's action_count + last_action."""
        with self._lock:
            self._manual_actions.insert(0, action)
            self._manual_actions = self._manual_actions[:self._max_manual_actions]
            mc = self._manual_control
            self._manual_control = ManualControl(
                engaged=mc.engaged,
                engaged_at=mc.engaged_at,
                released_at=mc.released_at,
                reason=mc.reason,
                engaged_by=mc.engaged_by,
                last_action=action,
                action_count=mc.action_count + 1,
            )

    def get_manual_actions(self, limit: int = 40) -> list[dict]:
        with self._lock:
            return list(self._manual_actions[:limit])

    # ---- Execution snapshot (procedural layer) ----------------------------
    def update_execution(self, **fields) -> ExecutionSnapshot:
        """Patch fields on the execution snapshot. Pass any subset of
        ExecutionSnapshot's fields. Pass ``field=None`` to clear it.

        Typical callers:
          - Operator's slot executor sets ``active_slot`` when a slot
            starts; clears it when the slot ends.
          - NINA capture-progress callback updates ``active_frame`` +
            ``active_action`` + ``slot_progress`` per-frame.
          - Hard-stop sets ``blocked_reason`` when execution pauses.
        """
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with self._lock:
            for k, v in fields.items():
                if hasattr(self._execution, k):
                    setattr(self._execution, k, v)
            self._execution.updated_at = now
            return self._execution

    def get_execution(self) -> ExecutionSnapshot:
        with self._lock:
            return self._execution

    def clear_execution(self) -> None:
        """Reset to all-None — useful when execution stops cleanly."""
        with self._lock:
            self._execution = ExecutionSnapshot(
                updated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z"
            )

    # ---- Pending decisions (deliberation narration) -----------------------
    def post_pending_decision(self, pd: PendingDecision) -> PendingDecision:
        """Publish a new deliberation. ATLAS calls this when it starts
        weighing a material change. The dashboard's Pending Decisions
        panel reads from get_pending_decisions(). If a decision with
        the same id already exists, it is replaced."""
        with self._lock:
            self._pending_decisions[pd.id] = pd
            return pd

    def resolve_pending_decision(self, decision_id: str,
                                   resolution: str = "resolved") -> bool:
        """Remove a pending decision. Called when ATLAS actually picks
        a verb (pause/swap/etc.) or the human overrides. Returns True
        if a matching decision was removed."""
        with self._lock:
            return self._pending_decisions.pop(decision_id, None) is not None

    def get_pending_decisions(self) -> list[PendingDecision]:
        with self._lock:
            return list(self._pending_decisions.values())

    # ---- The unified "Right Now" view -------------------------------------
    def get_right_now(self) -> dict:
        """Read-only aggregated snapshot. The single source of truth for
        ATLAS-the-LLM, the dashboard, and other agents.

        Three layers per the doctrine:
          - situational: what IS (verdict, weather, day phase, manual)
          - procedural: what SHOULD be (active slot, action, next thing)
          - strategic: what's WORTHWHILE (plan fit, advisories, campaigns)

        Plus blocked_reason and pending_decisions for the deliberation
        narration. All fields are JSON-safe.

        This method does not write anything. It reads existing slots
        under the lock and composes the view. Safe to call from any
        thread, including the asyncio event loop."""
        computed_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"

        # --- Snapshot under the lock (no IO, no logic here) ----------
        with self._lock:
            verdict = self._verdict
            assessment = self._assessment
            plan = self._tonight_plan
            review = self._session_review
            preflight = self._preflight
            execution = self._execution
            manual = self._manual_control
            pending = list(self._pending_decisions.values())
            review_phase = {
                "phase": self._review_phase,
                "review_id": self._review_phase_review_id,
                "updated_at": self._review_phase_updated_at,
            }

        # --- Day phase (cheap, no IO) — best-effort -----------------
        day_phase_d: dict | None = None
        minutes_dark_remaining: float | None = None
        minutes_until_next_phase: float | None = None
        try:
            from atlas.db.managers import ConfigManager
            site = ConfigManager.get_site()
            if site is not None:
                lat = float(getattr(site, "latitude", 0.0) or 0.0)
                lon = float(getattr(site, "longitude", 0.0) or 0.0)
                if lat or lon:
                    from atlas.astronomy.day_phase import current_phase
                    dp = current_phase(lat, lon)
                    day_phase_d = dp.to_jsonable()
                    minutes_dark_remaining = dp.minutes_of_dark_remaining
                    minutes_until_next_phase = dp.minutes_until_next_phase
        except Exception:
            day_phase_d = None

        # --- Plan stats ---------------------------------------------
        visible_targets = (plan or {}).get("visible_targets") or []
        scheduled_total_min = (plan or {}).get("scheduled_total_min")
        dark_window_min = (plan or {}).get("dark_window_min")
        fit_pct: float | None = None
        if dark_window_min and scheduled_total_min is not None:
            try:
                if float(dark_window_min) > 0:
                    fit_pct = round(
                        100.0 * float(scheduled_total_min) / float(dark_window_min),
                        1,
                    )
            except Exception:
                fit_pct = None

        # --- Advisory severity rollup --------------------------------
        adv_list = (review or {}).get("advisories") or []
        adv_counts = {"info": 0, "warning": 0, "critical": 0}
        for a in adv_list:
            s = a.get("severity")
            if s in adv_counts:
                adv_counts[s] += 1

        # --- Session active? Heuristic: execution has an active slot
        # OR the preflight verdict says session_active.
        session_active = bool(getattr(execution, "active_slot", None))
        if preflight and isinstance(preflight, dict):
            if preflight.get("session_active"):
                session_active = True

        # --- Compose ------------------------------------------------
        return {
            "computed_at": computed_at,
            "situational": {
                "verdict": (verdict.verdict if verdict else VERDICT_UNKNOWN),
                "verdict_reason": (verdict.reason if verdict else ""),
                "verdict_decided_at": (verdict.decided_at if verdict else None),
                "weather_summary": (assessment.summary if assessment else None),
                "weather_severity": (assessment.overall_severity if assessment else None),
                "weather_assessed_at": (assessment.assessed_at if assessment else None),
                "day_phase": day_phase_d,
                "minutes_of_dark_remaining": minutes_dark_remaining,
                "minutes_until_next_phase": minutes_until_next_phase,
                "manual_control": {
                    "engaged": manual.engaged,
                    "reason": manual.reason if manual.engaged else None,
                    "engaged_at": manual.engaged_at if manual.engaged else None,
                },
                "preflight_verdict": (preflight or {}).get("verdict") if preflight else None,
                "preflight_reason": (preflight or {}).get("reason") if preflight else None,
                "session_active": session_active,
            },
            "procedural": {
                "active_slot": execution.active_slot,
                "active_action": execution.active_action,
                "active_frame": execution.active_frame,
                "slot_progress": execution.slot_progress,
                "next_action": execution.next_action,
                "next_action_at": execution.next_action_at,
                "planned_session_end": execution.planned_session_end,
                "execution_updated_at": execution.updated_at or None,
            },
            "strategic": {
                "plan_present": plan is not None,
                "plan_built_at": (plan or {}).get("built_at"),
                "plan_reason": (plan or {}).get("reason"),
                "visible_target_count": len(visible_targets),
                "considered_count": (plan or {}).get("considered_count"),
                "scheduled_total_min": scheduled_total_min,
                "dark_window_min": dark_window_min,
                "fit_pct": fit_pct,
                "active_campaigns": (plan or {}).get("active_campaigns"),
                "advisory_count": len(adv_list),
                "advisory_counts": adv_counts,
                "review_phase": review_phase.get("phase"),
                "review_phase_updated_at": review_phase.get("updated_at"),
                "in_recovery": bool((plan or {}).get("in_recovery")),
                "fallback_to_catalog": (plan or {}).get("fallback_to_catalog"),
            },
            "blocked_reason": execution.blocked_reason,
            "pending_decisions": [pd.to_jsonable() for pd in pending],
            # Top-line summary string — convenient for the LLM and any
            # caller that just wants one line. Built last so it can
            # reference the layers above.
            "summary": _summarize_right_now(
                verdict=verdict,
                day_phase_d=day_phase_d,
                execution=execution,
                manual=manual,
                visible_target_count=len(visible_targets),
                plan_present=plan is not None,
                pending_count=len(pending),
            ),
        }


def _summarize_right_now(*, verdict, day_phase_d, execution, manual,
                            visible_target_count: int, plan_present: bool,
                            pending_count: int) -> str:
    """One-line human summary used by ``get_right_now()['summary']``."""
    bits: list[str] = []
    if manual and getattr(manual, "engaged", False):
        bits.append("MANUAL")
    v = (verdict.verdict if verdict else VERDICT_UNKNOWN)
    bits.append(f"verdict={v}")
    if day_phase_d:
        ph = day_phase_d.get("phase")
        if ph:
            bits.append(f"phase={ph}")
    slot = getattr(execution, "active_slot", None)
    if slot:
        tgt = (slot or {}).get("target_name") or "?"
        bits.append(f"slot={tgt}")
        if getattr(execution, "active_action", None):
            bits.append(f"doing={execution.active_action}")
    elif plan_present:
        bits.append(f"plan={visible_target_count} targets, idle")
    else:
        bits.append("no plan yet")
    if pending_count:
        bits.append(f"pending={pending_count}")
    if getattr(execution, "blocked_reason", None):
        bits.append(f"blocked={execution.blocked_reason}")
    return " | ".join(bits)


_state: _ObservatoryState | None = None


def get_state() -> _ObservatoryState:
    global _state
    if _state is None:
        _state = _ObservatoryState()
    return _state
