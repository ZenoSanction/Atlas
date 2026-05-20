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


class _ObservatoryState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._assessment: WeatherAssessment | None = None
        self._verdict: OperatorVerdict | None = None
        self._tonight_plan: dict | None = None
        self._archivist_last: dict | None = None
        self._oracle_last: dict | None = None
        # Per-agent live status. Mission Control reads from here.
        self._agent_status: dict[str, AgentLiveStatus] = {
            n: AgentLiveStatus(name=n)
            for n in ("planner", "critic", "operator", "archivist", "oracle")
        }
        # Inter-agent message ring buffer for the live flow column
        self._message_flow: list[dict] = []
        self._max_messages = 80

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
        a change and broadcast accordingly."""
        with self._lock:
            prev = self._verdict
            self._verdict = v
            return prev

    def get_verdict(self) -> OperatorVerdict | None:
        with self._lock:
            return self._verdict

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

    def get_message_flow(self, limit: int = 80) -> list[dict]:
        with self._lock:
            return list(self._message_flow[:limit])

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


_state: _ObservatoryState | None = None


def get_state() -> _ObservatoryState:
    global _state
    if _state is None:
        _state = _ObservatoryState()
    return _state
