"""Session planning workflow — deterministic chain of command.

The operator described the exact pipeline they want for every nightly
session:

  1. Planner  → Critic      : "here's tonight's plan, please review"
  2. Critic   → Operator    : "reviewed; weather/moon/hardware warnings"
  3. Operator → Oracle      : "given this plan + warnings, anything to revisit?"
  4. Oracle   → Operator    : "candidate revisits / extended integrations"
  5. Operator → Planner     : "decision: proceed | re-plan around X | cancel"
  6. Planner  → broadcast   : "session finalised" OR rebuild with constraints

Each step carries the accumulating context in a single ``SessionReview``
blob that travels via bus relays with ``payload={"phase": ..., "review": ...}``.
This file defines the data model + serialisation; the agent handlers in
critic.py / operator.py / oracle.py / planner.py implement the actual
logic of each phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


# ---- Phase enum (string constants — simpler than enum.Enum on the bus) ----

PHASE_PLAN_BUILT     = "plan_built"       # Planner → Critic
PHASE_CRITIC_REVIEW  = "critic_review"    # Critic → Operator
PHASE_ORACLE_QUERY   = "oracle_query"     # Operator → Oracle
PHASE_ORACLE_REVIEW  = "oracle_review"    # Oracle → Operator
PHASE_OPERATOR_DECN  = "operator_decision"  # Operator → Planner
PHASE_FINALISED      = "session_finalized"
PHASE_CANCELLED      = "session_cancelled"
PHASE_REPLAN         = "session_replan"

ALL_PHASES = [
    PHASE_PLAN_BUILT,
    PHASE_CRITIC_REVIEW,
    PHASE_ORACLE_QUERY,
    PHASE_ORACLE_REVIEW,
    PHASE_OPERATOR_DECN,
    PHASE_FINALISED,  # terminal
]

TERMINAL_PHASES = {PHASE_FINALISED, PHASE_CANCELLED, PHASE_REPLAN}


@dataclass
class SessionWarning:
    """One item from the Critic's review of a plan."""
    kind: str            # "weather" | "moon" | "hardware" | "calibration" | ...
    severity: str        # "ok" | "warning" | "critical"
    message: str
    target_name: str | None = None   # e.g. "M42" if it's a per-target moon issue
    suggested_constraint: str | None = None  # e.g. "avoid_moon", "avoid_low_alt"


@dataclass
class OracleSuggestion:
    """One revisit / extended-integration proposal."""
    target_name: str
    reason: str          # "campaign cadence due", "needs deeper integration", ...
    priority_bump: int = 0  # add this much to planner priority


@dataclass
class SessionReview:
    """Accumulated state for one trip through the session-planning pipeline.

    Created by the Planner on a fresh plan build. Each agent that touches
    it appends its findings then forwards via the bus until either the
    Operator finalises ('proceed') or asks the Planner to re-plan."""
    review_id: str
    plan: dict                   # The plan being reviewed (visible_targets, etc.)
    started_at: str              # ISO UTC
    phase: str = PHASE_PLAN_BUILT
    phase_history: list[dict] = field(default_factory=list)   # [{phase, agent, at, note}]
    critic_warnings: list[SessionWarning] = field(default_factory=list)
    oracle_suggestions: list[OracleSuggestion] = field(default_factory=list)
    operator_decision: str | None = None        # "proceed" | "replan" | "cancel"
    operator_constraints: list[str] = field(default_factory=list)
    operator_reason: str | None = None
    final_at: str | None = None

    def advance(self, new_phase: str, agent: str, note: str = "") -> None:
        self.phase_history.append({
            "phase": new_phase,
            "agent": agent,
            "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "note": note,
        })
        self.phase = new_phase
        if new_phase in TERMINAL_PHASES:
            self.final_at = self.phase_history[-1]["at"]

    def to_jsonable(self) -> dict:
        return {
            "review_id": self.review_id,
            "plan": self.plan,
            "started_at": self.started_at,
            "phase": self.phase,
            "phase_history": list(self.phase_history),
            "critic_warnings": [asdict(w) for w in self.critic_warnings],
            "oracle_suggestions": [asdict(s) for s in self.oracle_suggestions],
            "operator_decision": self.operator_decision,
            "operator_constraints": list(self.operator_constraints),
            "operator_reason": self.operator_reason,
            "final_at": self.final_at,
        }

    @classmethod
    def from_jsonable(cls, d: dict) -> "SessionReview":
        sr = cls(
            review_id=d["review_id"],
            plan=d.get("plan") or {},
            started_at=d["started_at"],
            phase=d.get("phase", PHASE_PLAN_BUILT),
        )
        sr.phase_history = list(d.get("phase_history") or [])
        sr.critic_warnings = [SessionWarning(**w) for w in d.get("critic_warnings") or []]
        sr.oracle_suggestions = [OracleSuggestion(**s) for s in d.get("oracle_suggestions") or []]
        sr.operator_decision = d.get("operator_decision")
        sr.operator_constraints = list(d.get("operator_constraints") or [])
        sr.operator_reason = d.get("operator_reason")
        sr.final_at = d.get("final_at")
        return sr


def new_review_id() -> str:
    """Compact unique-ish review id for the audit trail. Not security-critical."""
    import secrets
    return secrets.token_hex(4)
