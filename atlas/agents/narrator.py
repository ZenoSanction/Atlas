"""Deliberation narrator.

Doctrine: when ATLAS considers a material change, it does not just
act — it *deliberates aloud*. The deliberation appears in Pending
Decisions on the dashboard. While a pending decision is live:

  - The human can override it via the dashboard.
  - The deliberation has a timeout. If the timeout expires without
    resolution, ATLAS defaults to the safer action (which usually
    means: don't change anything, OR safe-NO-GO if a real risk has
    developed).
  - A higher-priority hard-stop pre-empts deliberation (storm cell,
    hardware fault — ATLAS acts immediately).

Why this matters: most autonomous systems are black boxes. They just
act, and the human sees the consequence. With pending-decision
narration, ATLAS's reasoning is exposed. The human can intervene
with better information ("don't pause — radar shows clearing in 10
min"), or let ATLAS finish its thinking.

Usage:

    from atlas.agents.narrator import deliberate, RESOLUTION_APPLIED

    result = await deliberate(
        verb="pause",
        reason="wind sustained above threshold for 5 min",
        evidence={"wind_mph": 22, "threshold": 20},
        narration=("Wind has climbed to 22 mph (threshold 20). "
                   "Watching for 5 min before pausing. Operator "
                   "override available."),
        decide_after_s=300,
        default_action="apply",   # or "skip"
        confidence_layer="rules",
    )
    if result.resolution == RESOLUTION_APPLIED:
        log.info(f"Deliberation applied: {result.adaptation.summary}")
    elif result.resolution == RESOLUTION_OVERRIDDEN:
        log.info(f"Operator overrode: {result.override_verb}")

Pre-emption: pass ``preempt_event`` (an asyncio.Event) for hard-stop
flows. When set, the narrator skips its wait and applies immediately
regardless of timeout.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4


# ---- Resolution outcomes ---------------------------------------------------

RESOLUTION_APPLIED      = "applied"           # default action ran
RESOLUTION_OVERRIDDEN   = "overridden"        # operator picked a different verb
RESOLUTION_CANCELLED    = "cancelled"         # operator dismissed without action
RESOLUTION_TIMEOUT_SKIP = "timeout_skip"      # timeout + default=skip
RESOLUTION_PREEMPTED    = "preempted"         # hard-stop fired


# ---- Override registry -----------------------------------------------------
#
# Each live PendingDecision gets an asyncio.Event the narrator awaits.
# The HTTP route or chat tool that resolves the decision sets the
# event and writes the override outcome into _OVERRIDES.

_OVERRIDE_EVENTS: dict[str, asyncio.Event] = {}
_OVERRIDES: dict[str, dict] = {}              # decision_id -> {action, verb, reason}
_REGISTRY_LOCK = asyncio.Lock()


@dataclass
class DeliberationResult:
    decision_id: str
    resolution: str                  # one of the RESOLUTION_* constants
    adaptation: object | None = None # AdaptationResult if applied
    override_verb: str | None = None
    override_reason: str | None = None
    deliberation_s: float = 0.0


# ---- The deliberation primitive --------------------------------------------


async def deliberate(*, verb: str, reason: str, narration: str,
                       evidence: dict | None = None,
                       decide_after_s: float = 300.0,
                       default_action: str = "apply",
                       confidence_layer: str = "rules",
                       severity: str = "info",
                       preempt_event: asyncio.Event | None = None,
                       verb_kwargs: dict | None = None,
                       ) -> DeliberationResult:
    """Post a pending decision, wait, then act.

    Parameters:
      verb              - the adaptation verb that ATLAS proposes (one of
                          the seven, or 'no_change' for purely informational
                          deliberations)
      reason            - plain-English justification
      narration         - the deliberation text shown to the operator
      evidence          - structured evidence backing the verb
      decide_after_s    - timeout before default_action fires (default 5 min)
      default_action    - 'apply' (run the verb on timeout) or 'skip'
                          (let timeout cancel without action)
      confidence_layer  - rules | history | llm | unresolved
      severity          - info | warning | critical (doctrine notification policy)
      preempt_event     - if set during the wait, apply immediately
      verb_kwargs       - extra kwargs to pass through to adapt_plan
                          (target_name, end_at_utc, replacement, etc.)

    Returns DeliberationResult with the resolution outcome.
    """
    from atlas.agents.state import PendingDecision, get_state
    from atlas.agents.plan_adapter import adapt_plan, AdaptationResult

    decision_id = uuid4().hex[:12]
    started_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    decide_by = (datetime.utcnow()
                   + timedelta(seconds=decide_after_s)
                   ).isoformat(timespec="seconds") + "Z"

    pd = PendingDecision(
        id=decision_id,
        kind=verb,
        narration=narration,
        started_at=started_at,
        decide_by=decide_by,
        default_action=default_action,
        evidence=evidence or {},
        confidence_layer=confidence_layer,
        severity=severity,
    )
    get_state().post_pending_decision(pd)

    # Register an override event before yielding
    ev = asyncio.Event()
    async with _REGISTRY_LOCK:
        _OVERRIDE_EVENTS[decision_id] = ev

    started_mono = asyncio.get_event_loop().time()

    try:
        # Wait for: override, preempt, or timeout
        waiters = [asyncio.create_task(ev.wait())]
        if preempt_event is not None:
            waiters.append(asyncio.create_task(preempt_event.wait()))

        try:
            done, pending = await asyncio.wait(
                waiters, timeout=decide_after_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        except Exception:
            done = set()

        elapsed = asyncio.get_event_loop().time() - started_mono

        # ---- Resolution ----
        if preempt_event is not None and preempt_event.is_set():
            # Hard-stop: apply immediately, regardless of timeout
            result = adapt_plan(verb, reason=f"preempted: {reason}",
                                  evidence=evidence or {}, **(verb_kwargs or {}))
            get_state().resolve_pending_decision(decision_id)
            return DeliberationResult(
                decision_id=decision_id,
                resolution=RESOLUTION_PREEMPTED,
                adaptation=result,
                deliberation_s=elapsed,
            )

        if ev.is_set():
            # Operator override resolved the decision
            override = _OVERRIDES.pop(decision_id, {}) or {}
            action = override.get("action", "cancel")
            get_state().resolve_pending_decision(decision_id)

            if action == "apply":
                # Operator confirmed — apply the verb ATLAS proposed
                result = adapt_plan(verb, reason=reason,
                                      evidence=evidence or {},
                                      **(verb_kwargs or {}))
                return DeliberationResult(
                    decision_id=decision_id,
                    resolution=RESOLUTION_APPLIED,
                    adaptation=result,
                    deliberation_s=elapsed,
                )
            elif action == "override":
                # Operator picked a different verb
                ov_verb = (override.get("verb") or "").strip()
                ov_reason = (override.get("reason") or "operator override").strip()
                ov_kwargs = override.get("kwargs") or {}
                if not ov_verb:
                    return DeliberationResult(
                        decision_id=decision_id,
                        resolution=RESOLUTION_CANCELLED,
                        deliberation_s=elapsed,
                    )
                result = adapt_plan(ov_verb, reason=ov_reason,
                                      evidence={"source": "operator_override",
                                                  **(evidence or {})},
                                      **ov_kwargs)
                return DeliberationResult(
                    decision_id=decision_id,
                    resolution=RESOLUTION_OVERRIDDEN,
                    adaptation=result,
                    override_verb=ov_verb,
                    override_reason=ov_reason,
                    deliberation_s=elapsed,
                )
            else:
                # cancel — dismiss without action
                return DeliberationResult(
                    decision_id=decision_id,
                    resolution=RESOLUTION_CANCELLED,
                    deliberation_s=elapsed,
                )

        # ---- Timeout ----
        get_state().resolve_pending_decision(decision_id)
        if default_action == "apply":
            result = adapt_plan(verb, reason=f"timeout-default: {reason}",
                                  evidence=evidence or {}, **(verb_kwargs or {}))
            return DeliberationResult(
                decision_id=decision_id,
                resolution=RESOLUTION_APPLIED,
                adaptation=result,
                deliberation_s=elapsed,
            )
        return DeliberationResult(
            decision_id=decision_id,
            resolution=RESOLUTION_TIMEOUT_SKIP,
            deliberation_s=elapsed,
        )
    finally:
        async with _REGISTRY_LOCK:
            _OVERRIDE_EVENTS.pop(decision_id, None)
            _OVERRIDES.pop(decision_id, None)


# ---- Resolution API (called by HTTP and chat tools) ------------------------


async def resolve(decision_id: str, *, action: str,
                    verb: str | None = None, reason: str | None = None,
                    kwargs: dict | None = None) -> bool:
    """Resolve a pending decision from the dashboard or chat.

    action:
      'apply'    - confirm ATLAS's proposed verb
      'override' - pick a different verb (provide verb/reason/kwargs)
      'cancel'   - dismiss without acting

    Returns True if a live decision was resolved, False if no matching
    decision was found (already resolved or never existed)."""
    async with _REGISTRY_LOCK:
        ev = _OVERRIDE_EVENTS.get(decision_id)
        if ev is None:
            # No active deliberation. If the decision is in state but
            # the narrator isn't watching, clear it anyway so the
            # dashboard stops showing it.
            from atlas.agents.state import get_state
            get_state().resolve_pending_decision(decision_id)
            return False
        _OVERRIDES[decision_id] = {
            "action": action,
            "verb": verb,
            "reason": reason,
            "kwargs": kwargs or {},
        }
        ev.set()
    return True


def active_decisions() -> list[str]:
    """Return the IDs of decisions currently being deliberated.
    Useful for tests and observability."""
    return list(_OVERRIDE_EVENTS.keys())
