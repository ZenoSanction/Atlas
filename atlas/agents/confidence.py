"""Confidence layer 1 — deterministic rules.

Doctrine: ATLAS uses three layers in order to decide whether it is
"confident enough" to act:

  Layer 1 - Deterministic rules. Always available, free, predictable.
            Handles 90%+ of operational decisions.
  Layer 2 - Pattern matching against session history. Cheap. Handles
            patterns the rules don't cover. (built later)
  Layer 3 - LLM judgment. Costs money. Opt-in only. (built later)

If all three fail to give clear confidence -> safe-NO-GO.

This module is Layer 1. Each rule is a pure function:

    rule(right_now: dict) -> Recommendation | None

Returning None means "this rule doesn't apply; try the next one."
Returning a Recommendation means "I'm confident — here's what to do
and why." The first rule to return a Recommendation wins.

If no rule matches, recommend() returns ``UNRESOLVED`` — the caller
escalates to Layer 2/3 or, if those are off, defaults to safe-NO-GO.

Adding a rule:
    1. Write a function (right_now: dict) -> Recommendation | None.
    2. Append it to RULES in priority order (first match wins).
    3. Keep rule logic small and pure; no IO, no DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


# ---- Recommendation shape --------------------------------------------------

@dataclass
class Recommendation:
    """A rule's verdict. The caller (the Operator's deliberation loop)
    feeds this straight into ``deliberate(verb=..., reason=..., ...)``."""
    verb: str                            # one of the seven adaptation verbs
    reason: str                          # plain-English justification
    evidence: dict = field(default_factory=dict)
    confidence_layer: str = "rules"
    severity: str = "info"               # info | warning | critical
    rule_name: str = ""                  # which rule matched
    verb_kwargs: dict = field(default_factory=dict)  # extras for adapt_plan

    def to_jsonable(self) -> dict:
        return {
            "verb": self.verb,
            "reason": self.reason,
            "evidence": self.evidence,
            "confidence_layer": self.confidence_layer,
            "severity": self.severity,
            "rule_name": self.rule_name,
            "verb_kwargs": self.verb_kwargs,
        }


UNRESOLVED = None  # what recommend() returns when no rule matches


# ---- Individual rules ------------------------------------------------------
#
# Each takes the right_now dict and returns Recommendation | None. The
# order in RULES is the priority order — earlier = higher priority.
# Rules consult ONLY the right_now dict so they are fully testable
# without spinning up agents.

def _rule_manual_control(rn: dict) -> Optional[Recommendation]:
    """If the human has 'taken control', autonomous rules stand down.

    The Operator agent already short-circuits its autonomous dispatch
    when manual is engaged — this rule makes that visible to the
    confidence engine so it doesn't accidentally produce a
    recommendation that overlaps with manual operation."""
    mc = (rn.get("situational") or {}).get("manual_control") or {}
    if mc.get("engaged"):
        # No recommendation; let the human drive.
        return Recommendation(
            verb="no_change",
            reason="manual control engaged; standing down",
            confidence_layer="rules",
            rule_name="manual_control",
            severity="info",
        )
    return None


def _rule_weather_critical(rn: dict) -> Optional[Recommendation]:
    """Critical weather severity (storm cell, dangerous wind/humidity
    forecast crossing safety threshold) -> safe_shutdown.

    Doctrine: equipment safety comes before everything. A critical
    weather assessment is an automatic safe-NO-GO trigger."""
    sit = rn.get("situational") or {}
    sev = sit.get("weather_severity")
    if sev == "critical":
        return Recommendation(
            verb="safe_shutdown",
            reason=f"weather critical: {sit.get('weather_summary') or ''}",
            evidence={"weather_severity": sev,
                      "weather_summary": sit.get("weather_summary")},
            confidence_layer="rules",
            severity="critical",
            rule_name="weather_critical",
        )
    return None


def _rule_nogo_sustained(rn: dict) -> Optional[Recommendation]:
    """Verdict went NO-GO and dwell remaining > 30 min while a session
    is active -> pause.

    Doctrine example: 'Verdict went NO-GO, hysteresis met, dwell still
    > 30 min -> pause.' The hysteresis side of the doctrine is enforced
    by the verdict watcher itself (it doesn't write NO-GO until the
    hysteresis window is met); by the time this rule sees NO-GO it
    has already settled."""
    sit = rn.get("situational") or {}
    proc = rn.get("procedural") or {}
    strat = rn.get("strategic") or {}
    verdict = sit.get("verdict")
    session_active = sit.get("session_active")
    if verdict == "NO-GO" and session_active and not proc.get("blocked_reason"):
        # Check there is meaningful dwell remaining (>= 30 min)
        dark_min = sit.get("minutes_of_dark_remaining")
        if dark_min is None or dark_min >= 30:
            return Recommendation(
                verb="pause",
                reason=(f"verdict NO-GO sustained ({sit.get('verdict_reason') or ''}); "
                          f"{int(dark_min) if dark_min else '?'} min of dark remaining"),
                evidence={"verdict": verdict,
                          "verdict_reason": sit.get("verdict_reason"),
                          "minutes_of_dark_remaining": dark_min},
                confidence_layer="rules",
                severity="warning",
                rule_name="nogo_sustained",
            )
    return None


def _rule_go_after_pause(rn: dict) -> Optional[Recommendation]:
    """Verdict returned to GO while execution is paused -> resume.

    Mirror of the previous rule. The blocked_reason is set when
    execution was paused (whether by ATLAS or hard-stop); when the
    verdict clears, we want to resume."""
    sit = rn.get("situational") or {}
    blocked = (rn.get("blocked_reason") or "").lower()
    if sit.get("verdict") == "GO" and blocked.startswith("paused"):
        return Recommendation(
            verb="resume",
            reason=f"verdict GO; clearing pause ({rn.get('blocked_reason')})",
            evidence={"verdict": "GO",
                      "previous_block": rn.get("blocked_reason")},
            confidence_layer="rules",
            severity="info",
            rule_name="go_after_pause",
        )
    return None


def _rule_active_window_expired(rn: dict) -> Optional[Recommendation]:
    """Active slot's window expired while we were paused -> drop_slot.

    Detected by: active_slot is set, but the next_action_at or the
    active slot's end_utc is in the past. (The actual time comparison
    is deferred to the rule's reading of execution.planned_session_end
    and the active_slot end_utc, both already in the right_now view.)

    Note: we conservatively only drop the *current* slot — the slot
    executor's next tick will pick up the next slot in the plan."""
    proc = rn.get("procedural") or {}
    slot = proc.get("active_slot")
    if not slot:
        return None
    end_utc = (slot or {}).get("end_utc")
    if not end_utc:
        return None
    from datetime import datetime
    try:
        end_dt = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
    except Exception:
        return None
    now = datetime.utcnow().replace(tzinfo=end_dt.tzinfo)
    if end_dt < now:
        return Recommendation(
            verb="drop_slot",
            reason=f"active slot window expired ({end_utc})",
            evidence={"target_name": slot.get("target_name"),
                      "end_utc": end_utc},
            verb_kwargs={"target_name": slot.get("target_name")},
            confidence_layer="rules",
            severity="info",
            rule_name="active_window_expired",
        )
    return None


def _rule_dawn_approaching(rn: dict) -> Optional[Recommendation]:
    """Less than safe-startup-overhead of dark remaining -> truncate.

    Don't let the slot executor try to spin up another target when
    there's no useful imaging window left.
    """
    sit = rn.get("situational") or {}
    proc = rn.get("procedural") or {}
    dark_min = sit.get("minutes_of_dark_remaining")
    if dark_min is None or dark_min > 20:
        return None
    if not sit.get("session_active"):
        return None
    # Find the planned end. If already set, no new action.
    if proc.get("planned_session_end"):
        return None
    return Recommendation(
        verb="truncate",
        reason=f"only {dark_min:.0f} min of dark remaining; closing out",
        evidence={"minutes_of_dark_remaining": dark_min},
        verb_kwargs={"after_slot":
                     (proc.get("active_slot") or {}).get("target_name") or None},
        confidence_layer="rules",
        severity="info",
        rule_name="dawn_approaching",
    )


# ---- Rule registry (priority order: first match wins) ----------------------

RULES: list[Callable[[dict], Optional[Recommendation]]] = [
    _rule_manual_control,          # always defer to human if manual
    _rule_weather_critical,        # safety first
    _rule_nogo_sustained,          # then act on sustained NO-GO
    _rule_go_after_pause,          # mirror: clear pause when GO returns
    _rule_active_window_expired,   # housekeeping: drop expired slots
    _rule_dawn_approaching,        # housekeeping: wind down before dawn
]


# ---- Public entry point ----------------------------------------------------


def recommend(right_now: dict) -> Recommendation | None:
    """Run the rule chain against the Right Now snapshot.

    Returns the first matching Recommendation, or None ('UNRESOLVED')
    if no rule applies. Layer 2/3 escalation is the caller's job;
    when those are off and Layer 1 is UNRESOLVED, the doctrine says
    'when in doubt, stop' -> safe_shutdown.

    The caller (typically the Operator's autonomous loop) feeds the
    Recommendation into the narrator's ``deliberate(...)`` so the
    decision appears in Pending Decisions and the human can override
    before the timeout fires."""
    for rule in RULES:
        try:
            r = rule(right_now)
        except Exception:
            # A buggy rule must not take down the engine.
            continue
        if r is not None:
            return r
    return UNRESOLVED


def safe_default_when_unresolved(right_now: dict) -> Recommendation:
    """When Layers 1/2/3 all fail to give clear confidence, doctrine
    says default to safe-NO-GO. This helper produces the canonical
    recommendation for that case so callers don't reinvent it."""
    sit = right_now.get("situational") or {}
    return Recommendation(
        verb="safe_shutdown",
        reason=("no confident recommendation available; defaulting to "
                "safe-NO-GO per doctrine"),
        evidence={"verdict": sit.get("verdict"),
                  "weather_severity": sit.get("weather_severity"),
                  "manual": sit.get("manual_control", {}).get("engaged")},
        confidence_layer="unresolved",
        severity="critical",
        rule_name="safe_default",
    )
