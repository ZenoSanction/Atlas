"""Session plan + advisory annotations.

The Planner builds the plan and publishes it READY. Period. The plan
exists regardless of weather, hardware state, or anything else —
making the plan and executing the plan are two different things.

Plan states:
  building   - Planner is still computing (very brief, often skipped)
  ready      - Plan is built and stays this way until superseded
  replanned  - A newer plan exists; this one is historical

The Critic and Oracle still run their checks. Their findings get
appended as advisories[] for the operator to read. Advisories never
change the plan state — they're purely informational about *what's
going on around the plan*, not the plan itself.

Execution authorization is a separate concept handled elsewhere
(atlas.agents.state.OperatorVerdict — the GO / CAUTION / NO-GO
banner). Hard-stop conditions (storm rolling in, wind beyond
critical, hardware fault) flip the verdict to NO-GO, which gates
session execution. They DO NOT touch the plan. When weather clears,
the verdict flips back to GO/CAUTION and execution resumes against
the same plan — no rebuild required.

Backward-compat: the old PHASE_* constants and STATE_HARD_STOP are
kept as aliases for any in-flight references in chat history /
decision logs. New code should use STATE_READY / STATE_REPLANNED.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


# ---- States the plan can be in --------------------------------------------

STATE_BUILDING   = "building"     # Planner is still computing (very brief)
STATE_READY      = "ready"        # Plan published; stays this way until superseded
STATE_REPLANNED  = "replanned"    # A newer plan exists; this one is historical

ALL_STATES = [STATE_BUILDING, STATE_READY, STATE_REPLANNED]
TERMINAL_STATES = {STATE_REPLANNED}

# Legacy state name. Kept so old log rows / decisions deserialise. New
# code should never write this — execution-blocking lives on the
# OperatorVerdict, not the plan.
STATE_HARD_STOP  = "hard_stop"


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
    finalized_at: str | None = None
    # Audit trail — what events happened against this plan, in order.
    history: list[dict] = field(default_factory=list)

    def add_advisory(self, advisory: Advisory) -> None:
        """Append an advisory and record the event in the audit history.

        Advisories never change the plan state. They're informational —
        the operator reads them on the dashboard. Execution gating
        (storm, equipment risk) is handled by the OperatorVerdict
        separately."""
        self.advisories.append(advisory)
        self.history.append({
            "kind": "advisory",
            "at": advisory.at,
            "source": advisory.source,
            "severity": advisory.severity,
            "message": advisory.message[:160],
        })

    def replanned(self, reason: str = "rebuild") -> None:
        """Mark this plan as superseded. The Planner does this right
        before publishing a fresh plan so the old one is clearly
        retired in the history."""
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
            "finalized_at": self.finalized_at,
            "history": list(self.history),
        }

    @classmethod
    def from_jsonable(cls, d: dict) -> "SessionPlanState":
        # Legacy compatibility: any plan blob written before the
        # decoupling refactor may have state="hard_stop". Coerce to
        # ready — that plan is just as usable now as it was then;
        # execution gating moved to the OperatorVerdict.
        legacy_state = d.get("state", STATE_READY)
        if legacy_state == STATE_HARD_STOP:
            legacy_state = STATE_READY
        s = cls(
            review_id=d["review_id"],
            plan=d.get("plan") or {},
            started_at=d["started_at"],
            state=legacy_state,
        )
        s.advisories = [Advisory(**a) for a in d.get("advisories") or []]
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
