"""Session plan + advisory annotations.

ORIGINAL DESIGN (now removed): A six-stage gated pipeline ran every
plan rebuild — Planner → Critic → Operator → Oracle → Operator →
Planner — and only published a plan after every agent approved.
That held perfectly good plans hostage to advisory warnings and
slowed the dashboard's "tonight's targets" view by however long the
slowest agent took to file its review.

CURRENT DESIGN: The Planner builds the plan and **publishes it
immediately** in the FINALIZED state. The plan is usable the moment
it's built. The Critic and Oracle still run their checks, but they
do so in parallel as *advisors* — their findings get appended to
the plan's ``advisories[]`` list asynchronously, and the dashboard
shows them as inline annotations.

The Operator only intervenes for HARD-STOP conditions — things that
would damage equipment or wipe out the entire night:

  - precipitation > 0 in current weather (storm)
  - wind > critical threshold (mount/scope at risk)
  - sustained 100% cloud cover across the entire dark window
    (no point opening the roof)
  - critical hardware fault (camera disconnect, mount park failure)

Everything else is an advisory the operator reads on the dashboard
and decides what to do about. The new model trusts the operator to
make those judgement calls; it doesn't pre-empt them.

Backward-compat note: the old PHASE_* constants are kept as aliases
for any in-flight references in chat history / decision logs. New
code should use ``Advisory`` and ``SessionPlanState``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


# ---- States the plan can be in --------------------------------------------

STATE_BUILDING   = "building"     # Planner is still computing (very brief)
STATE_READY      = "ready"        # Plan published, advisories accumulating
STATE_HARD_STOP  = "hard_stop"    # Operator cancelled — storm / damage risk
STATE_REPLANNED  = "replanned"    # Superseded by a fresh rebuild

ALL_STATES = [STATE_BUILDING, STATE_READY, STATE_HARD_STOP, STATE_REPLANNED]
TERMINAL_STATES = {STATE_HARD_STOP, STATE_REPLANNED}


# ---- Legacy phase aliases (kept so old log rows still resolve) ------------
# These names appear in DecisionManager rows + chat history from before the
# advisory refactor. Don't reference them in new code.
PHASE_PLAN_BUILT     = "plan_built"
PHASE_CRITIC_REVIEW  = "critic_review"
PHASE_ORACLE_QUERY   = "oracle_query"
PHASE_ORACLE_REVIEW  = "oracle_review"
PHASE_OPERATOR_DECN  = "operator_decision"
PHASE_FINALISED      = "session_finalized"
PHASE_CANCELLED      = "session_cancelled"
PHASE_REPLAN         = "session_replan"


@dataclass
class Advisory:
    """One advisory note attached to a plan.

    Severity guidance:
      "info"     — purely informational (oracle revisit suggestion,
                   calibration library staleness)
      "warning"  — operator should know (moon proximity, wind ramping,
                   dew margin tight)
      "critical" — would normally be a hard-stop but the Operator hasn't
                   acted yet; the dashboard surfaces this loudly.

    Hard-stops aren't advisories — they flip the plan's state to
    STATE_HARD_STOP directly.
    """
    kind: str                   # "weather" | "moon" | "calibration" | "oracle" | ...
    severity: str               # "info" | "warning" | "critical"
    message: str                # human-readable, shown on dashboard
    source: str                 # "critic" | "oracle" | "operator" | "preflight"
    at: str                     # ISO UTC when this advisory was filed
    target_name: str | None = None       # optional: per-target advisory
    suggested_constraint: str | None = None  # optional: hint for next rebuild


@dataclass
class SessionPlanState:
    """One plan, with whatever advisories have landed against it so far.

    Created by the Planner on every _rebuild_plan(). Immediately published
    with state = STATE_READY. The Critic and Oracle (running in their
    own tasks) add advisories afterward as they finish their checks.

    The dashboard reads this directly — `state`, `plan`, `advisories[]`.
    """
    review_id: str
    plan: dict
    started_at: str
    state: str = STATE_READY
    advisories: list[Advisory] = field(default_factory=list)
    hard_stop_reason: str | None = None
    finalized_at: str | None = None
    # Audit trail — what events happened against this plan, in order.
    history: list[dict] = field(default_factory=list)

    def add_advisory(self, advisory: Advisory) -> None:
        """Append an advisory and record the event in the audit history."""
        self.advisories.append(advisory)
        self.history.append({
            "kind": "advisory",
            "at": advisory.at,
            "source": advisory.source,
            "severity": advisory.severity,
            "message": advisory.message[:160],
        })

    def hard_stop(self, reason: str, source: str = "operator") -> None:
        """Flip the plan into the HARD_STOP terminal state."""
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        self.state = STATE_HARD_STOP
        self.hard_stop_reason = reason
        self.finalized_at = now
        self.history.append({
            "kind": "hard_stop", "at": now,
            "source": source, "reason": reason,
        })

    def replanned(self, reason: str = "rebuild") -> None:
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        self.state = STATE_REPLANNED
        self.finalized_at = now
        self.history.append({"kind": "replanned", "at": now, "reason": reason})

    # Convenience aggregators for the dashboard --------------------------------

    def severity_counts(self) -> dict[str, int]:
        c = {"info": 0, "warning": 0, "critical": 0}
        for a in self.advisories:
            if a.severity in c:
                c[a.severity] += 1
        return c

    def has_critical_advisory(self) -> bool:
        return any(a.severity == "critical" for a in self.advisories)

    def to_jsonable(self) -> dict:
        return {
            "review_id": self.review_id,
            "plan": self.plan,
            "started_at": self.started_at,
            "state": self.state,
            "advisories": [asdict(a) for a in self.advisories],
            "advisory_counts": self.severity_counts(),
            "hard_stop_reason": self.hard_stop_reason,
            "finalized_at": self.finalized_at,
            "history": list(self.history),
        }

    @classmethod
    def from_jsonable(cls, d: dict) -> "SessionPlanState":
        s = cls(
            review_id=d["review_id"],
            plan=d.get("plan") or {},
            started_at=d["started_at"],
            state=d.get("state", STATE_READY),
        )
        s.advisories = [Advisory(**a) for a in d.get("advisories") or []]
        s.hard_stop_reason = d.get("hard_stop_reason")
        s.finalized_at = d.get("finalized_at")
        s.history = list(d.get("history") or [])
        return s


def new_review_id() -> str:
    """Compact unique-ish id for the audit trail."""
    import secrets
    return secrets.token_hex(4)


# ---- Legacy data-class aliases ---------------------------------------------
# The old SessionReview / SessionWarning / OracleSuggestion structures are
# kept as type aliases so any code path still importing them doesn't break
# during the cutover. They map to the new model 1:1.

SessionReview = SessionPlanState
SessionWarning = Advisory       # alias — slightly different shape but read-compat
OracleSuggestion = Advisory     # alias — same
