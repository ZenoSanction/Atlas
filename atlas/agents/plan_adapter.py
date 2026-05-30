"""The seven adaptation verbs.

These are the mechanical operations ATLAS may perform on a live plan
in response to a material change. They are not permission-gated; any
can be chosen based on the situation and ATLAS's confidence. The
set is intentionally small — each verb has clear semantics and clear
preconditions.

    pause          Hold execution. No plan change. Resume when conditions allow.
    resume         Pick the plan back up where it stopped.
    drop_slot      Skip a slot whose window expired or whose target is no
                   longer reachable.
    truncate       End the session early at a specific time.
    swap           Replace the target in a slot with another from the same
                   campaign or workflow.
    insert         Squeeze in a high-priority new slot.
    safe_shutdown  Park mount, warm camera, close roof, notify operator.
                   Default when uncertain.

Doctrine:
    - The plan keeps its identity. Same review_id; version bumps.
    - Every adaptation appends a diff entry to version_history.
    - Adapt within the plan beats rebuild the plan beats abandon the plan.

Usage:

    from atlas.agents.plan_adapter import adapt_plan, AdaptationResult

    result = adapt_plan("pause", reason="wind sustained over threshold",
                        evidence={"wind_mph": 22})
    if result.ok:
        log.info(result.summary)
    else:
        log.warning(f"pause rejected: {result.reason}")

The module operates on shared state only — reads the current plan
from get_state(), writes back through set_tonight_plan(). Callers
do not pass the plan in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from atlas.agents.plan_version import bump_for_adaptation


# Seven verbs (canonical names)
VERB_PAUSE          = "pause"
VERB_RESUME         = "resume"
VERB_DROP_SLOT      = "drop_slot"
VERB_TRUNCATE       = "truncate"
VERB_SWAP           = "swap"
VERB_INSERT         = "insert"
VERB_SAFE_SHUTDOWN  = "safe_shutdown"

ALL_VERBS = (
    VERB_PAUSE, VERB_RESUME, VERB_DROP_SLOT, VERB_TRUNCATE,
    VERB_SWAP, VERB_INSERT, VERB_SAFE_SHUTDOWN,
)


@dataclass
class AdaptationResult:
    """Return value from adapt_plan(). The dashboard renders this
    verbatim; logs serialize it; the morning report aggregates it."""
    ok: bool
    verb: str
    reason: str
    summary: str = ""                # one-line human description
    new_version: int | None = None
    diff: dict = field(default_factory=dict)
    error: str | None = None         # set when ok=False

    def to_jsonable(self) -> dict:
        return {
            "ok": self.ok,
            "verb": self.verb,
            "reason": self.reason,
            "summary": self.summary,
            "new_version": self.new_version,
            "diff": self.diff,
            "error": self.error,
        }


# ---- Public entry point ----------------------------------------------------


def adapt_plan(verb: str, *, reason: str, evidence: dict | None = None,
                  **kwargs) -> AdaptationResult:
    """Apply one of the seven adaptation verbs to the live plan.

    kwargs vary per verb — see individual handlers below. Common ones:
      drop_slot: target_name=str OR slot_index=int
      truncate:  end_at_utc=str (ISO) OR after_slot=str (target_name)
      swap:      target_name=str, replacement=dict (slot payload)
      insert:    slot=dict, at_index=int|None
      pause/resume/safe_shutdown: no extra kwargs needed
    """
    if verb not in ALL_VERBS:
        return AdaptationResult(ok=False, verb=verb, reason=reason,
                                  error=f"unknown verb: {verb}")
    handler = _VERBS[verb]
    try:
        return handler(reason=reason, evidence=evidence or {}, **kwargs)
    except Exception as e:  # pragma: no cover - defensive
        return AdaptationResult(ok=False, verb=verb, reason=reason,
                                  error=f"{type(e).__name__}: {e}")


# ---- Individual verbs ------------------------------------------------------


def _pause(*, reason: str, evidence: dict, **_) -> AdaptationResult:
    """Hold execution. The plan itself does not change — the execution
    snapshot's blocked_reason is set, and a version-history entry is
    appended noting the pause. Resume restores execution authorization
    without touching the slot list."""
    from atlas.agents.state import get_state
    st = get_state()
    plan = st.get_tonight_plan()
    if not plan:
        return AdaptationResult(ok=False, verb=VERB_PAUSE, reason=reason,
                                  error="no plan to pause")
    plan = bump_for_adaptation(plan, verb=VERB_PAUSE, reason=reason,
                                  diff={"paused_at": _now_iso(),
                                          "evidence": evidence})
    st.set_tonight_plan(plan)
    st.update_execution(blocked_reason=f"paused: {reason}")
    return AdaptationResult(
        ok=True, verb=VERB_PAUSE, reason=reason,
        summary=f"Paused execution ({reason}).",
        new_version=plan.get("version"),
        diff={"paused_at": _now_iso()},
    )


def _resume(*, reason: str, evidence: dict, **_) -> AdaptationResult:
    """Pick the plan back up. Clears blocked_reason; the slot executor
    on its next tick will check active_slot and dispatch."""
    from atlas.agents.state import get_state
    st = get_state()
    plan = st.get_tonight_plan()
    if not plan:
        return AdaptationResult(ok=False, verb=VERB_RESUME, reason=reason,
                                  error="no plan to resume")
    plan = bump_for_adaptation(plan, verb=VERB_RESUME, reason=reason,
                                  diff={"resumed_at": _now_iso(),
                                          "evidence": evidence})
    st.set_tonight_plan(plan)
    st.update_execution(blocked_reason=None)
    return AdaptationResult(
        ok=True, verb=VERB_RESUME, reason=reason,
        summary=f"Resumed execution ({reason}).",
        new_version=plan.get("version"),
        diff={"resumed_at": _now_iso()},
    )


def _drop_slot(*, reason: str, evidence: dict,
                  target_name: str | None = None,
                  slot_index: int | None = None,
                  **_) -> AdaptationResult:
    """Remove a slot whose window expired or target is unreachable.

    Locate by target_name (preferred) or slot_index (0-based). If
    neither matches, fail without modifying the plan."""
    from atlas.agents.state import get_state
    st = get_state()
    plan = st.get_tonight_plan()
    if not plan:
        return AdaptationResult(ok=False, verb=VERB_DROP_SLOT, reason=reason,
                                  error="no plan to drop from")

    targets = list(plan.get("visible_targets") or [])
    if not targets:
        return AdaptationResult(ok=False, verb=VERB_DROP_SLOT, reason=reason,
                                  error="plan has no slots")

    idx = _resolve_index(targets, target_name=target_name,
                            slot_index=slot_index)
    if idx is None:
        return AdaptationResult(ok=False, verb=VERB_DROP_SLOT, reason=reason,
                                  error="slot not found")

    dropped = targets.pop(idx)
    plan["visible_targets"] = targets
    diff = {"dropped": [dropped.get("target_name") or "?"],
            "index": idx, "evidence": evidence}
    plan = bump_for_adaptation(plan, verb=VERB_DROP_SLOT,
                                  reason=reason, diff=diff)
    st.set_tonight_plan(plan)
    return AdaptationResult(
        ok=True, verb=VERB_DROP_SLOT, reason=reason,
        summary=f"Dropped slot {dropped.get('target_name')!r} ({reason}).",
        new_version=plan.get("version"),
        diff=diff,
    )


def _truncate(*, reason: str, evidence: dict,
                 end_at_utc: str | None = None,
                 after_slot: str | None = None,
                 **_) -> AdaptationResult:
    """End the session early. Either at a wall-clock UTC time (drop any
    slot starting at/after that time) OR after a named slot completes
    (drop everything past it). Sets planned_session_end on the
    execution snapshot so the slot executor can stop dispatching."""
    from atlas.agents.state import get_state
    st = get_state()
    plan = st.get_tonight_plan()
    if not plan:
        return AdaptationResult(ok=False, verb=VERB_TRUNCATE, reason=reason,
                                  error="no plan to truncate")

    targets = list(plan.get("visible_targets") or [])
    new_targets = targets
    dropped_names: list[str] = []

    if end_at_utc:
        keep, drop = [], []
        for t in targets:
            if (t.get("start_utc") or "") >= end_at_utc:
                drop.append(t)
            else:
                keep.append(t)
        new_targets, dropped_names = keep, [t.get("target_name") or "?" for t in drop]
    elif after_slot:
        cut_after = None
        for i, t in enumerate(targets):
            if t.get("target_name") == after_slot:
                cut_after = i
                break
        if cut_after is None:
            return AdaptationResult(ok=False, verb=VERB_TRUNCATE, reason=reason,
                                      error=f"slot {after_slot!r} not in plan")
        new_targets = targets[:cut_after + 1]
        dropped_names = [t.get("target_name") or "?" for t in targets[cut_after + 1:]]
    else:
        return AdaptationResult(ok=False, verb=VERB_TRUNCATE, reason=reason,
                                  error="need end_at_utc or after_slot")

    plan["visible_targets"] = new_targets
    diff = {"truncated_at": end_at_utc or f"after {after_slot}",
            "dropped": dropped_names,
            "evidence": evidence}
    plan = bump_for_adaptation(plan, verb=VERB_TRUNCATE,
                                  reason=reason, diff=diff)
    st.set_tonight_plan(plan)
    # Reflect on execution snapshot
    if end_at_utc:
        st.update_execution(planned_session_end=end_at_utc)
    return AdaptationResult(
        ok=True, verb=VERB_TRUNCATE, reason=reason,
        summary=f"Truncated session ({reason}); "
                  f"dropped {len(dropped_names)} slot(s).",
        new_version=plan.get("version"),
        diff=diff,
    )


def _swap(*, reason: str, evidence: dict,
             target_name: str | None = None,
             slot_index: int | None = None,
             replacement: dict | None = None,
             **_) -> AdaptationResult:
    """Replace a slot's target with another. The replacement dict
    follows the visible_target shape (target_name, workflow, ra_deg,
    dec_deg, start_utc, end_utc, priority, ...). Preserves the
    original time window unless the replacement specifies its own."""
    from atlas.agents.state import get_state
    st = get_state()
    plan = st.get_tonight_plan()
    if not plan:
        return AdaptationResult(ok=False, verb=VERB_SWAP, reason=reason,
                                  error="no plan to swap in")
    if not replacement or not replacement.get("target_name"):
        return AdaptationResult(ok=False, verb=VERB_SWAP, reason=reason,
                                  error="replacement required (with target_name)")

    targets = list(plan.get("visible_targets") or [])
    idx = _resolve_index(targets, target_name=target_name,
                            slot_index=slot_index)
    if idx is None:
        return AdaptationResult(ok=False, verb=VERB_SWAP, reason=reason,
                                  error="slot not found")

    old = targets[idx]
    new = dict(old)
    # Replacement overrides fields it specifies; window is preserved
    # unless replacement carries its own start_utc/end_utc.
    for k, v in replacement.items():
        new[k] = v
    # If caller did not override window, keep old window.
    new.setdefault("start_utc", old.get("start_utc"))
    new.setdefault("end_utc", old.get("end_utc"))
    targets[idx] = new
    plan["visible_targets"] = targets

    diff = {"swapped": [old.get("target_name") or "?",
                          new.get("target_name") or "?"],
            "index": idx,
            "evidence": evidence}
    plan = bump_for_adaptation(plan, verb=VERB_SWAP,
                                  reason=reason, diff=diff)
    st.set_tonight_plan(plan)
    return AdaptationResult(
        ok=True, verb=VERB_SWAP, reason=reason,
        summary=f"Swapped {old.get('target_name')!r} -> "
                  f"{new.get('target_name')!r} ({reason}).",
        new_version=plan.get("version"),
        diff=diff,
    )


def _insert(*, reason: str, evidence: dict,
               slot: dict | None = None,
               at_index: int | None = None,
               **_) -> AdaptationResult:
    """Squeeze in a high-priority new slot (Oracle finds a time-critical
    transient, for example). Position with at_index; default is index
    0 (insert at front, ahead of the current/next slot). Slot dict
    must include at least target_name + start_utc + end_utc."""
    from atlas.agents.state import get_state
    st = get_state()
    plan = st.get_tonight_plan()
    if not plan:
        return AdaptationResult(ok=False, verb=VERB_INSERT, reason=reason,
                                  error="no plan to insert into")
    if not slot or not slot.get("target_name"):
        return AdaptationResult(ok=False, verb=VERB_INSERT, reason=reason,
                                  error="slot dict required with target_name")

    targets = list(plan.get("visible_targets") or [])
    if at_index is None:
        at_index = 0
    at_index = max(0, min(int(at_index), len(targets)))
    targets.insert(at_index, slot)
    plan["visible_targets"] = targets

    diff = {"inserted": slot.get("target_name") or "?",
            "at_index": at_index,
            "evidence": evidence}
    plan = bump_for_adaptation(plan, verb=VERB_INSERT,
                                  reason=reason, diff=diff)
    st.set_tonight_plan(plan)
    return AdaptationResult(
        ok=True, verb=VERB_INSERT, reason=reason,
        summary=f"Inserted {slot.get('target_name')!r} at index "
                  f"{at_index} ({reason}).",
        new_version=plan.get("version"),
        diff=diff,
    )


def _safe_shutdown(*, reason: str, evidence: dict, **_) -> AdaptationResult:
    """Park mount, warm camera, close roof, notify operator. Default
    when uncertain. The actual hardware sequencing is wired in the
    Operator/ATLAS agent — this verb's job is to:
      1. Mark the plan version bump so the audit trail shows it.
      2. Set blocked_reason so execution is held.
      3. Signal via review state for the dashboard."""
    from atlas.agents.state import get_state
    st = get_state()
    plan = st.get_tonight_plan() or {}
    plan = bump_for_adaptation(plan, verb=VERB_SAFE_SHUTDOWN,
                                  reason=reason,
                                  diff={"shutdown_at": _now_iso(),
                                          "evidence": evidence})
    st.set_tonight_plan(plan)
    st.update_execution(blocked_reason=f"safe-shutdown: {reason}",
                          active_slot=None, active_action=None,
                          active_frame=None)
    return AdaptationResult(
        ok=True, verb=VERB_SAFE_SHUTDOWN, reason=reason,
        summary=f"Safe shutdown initiated ({reason}).",
        new_version=plan.get("version"),
        diff={"shutdown_at": _now_iso()},
    )


# ---- Helpers ----------------------------------------------------------------


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _resolve_index(targets: list[dict], *,
                      target_name: str | None,
                      slot_index: int | None) -> int | None:
    """Locate a slot by name first (case-insensitive exact match),
    falling back to slot_index. Returns None if no match."""
    if target_name:
        wanted = target_name.strip().lower()
        for i, t in enumerate(targets):
            if (t.get("target_name") or "").strip().lower() == wanted:
                return i
    if slot_index is not None:
        try:
            si = int(slot_index)
        except (TypeError, ValueError):
            return None
        if 0 <= si < len(targets):
            return si
    return None


_VERBS: dict[str, Callable[..., AdaptationResult]] = {
    VERB_PAUSE:          _pause,
    VERB_RESUME:         _resume,
    VERB_DROP_SLOT:      _drop_slot,
    VERB_TRUNCATE:       _truncate,
    VERB_SWAP:           _swap,
    VERB_INSERT:         _insert,
    VERB_SAFE_SHUTDOWN:  _safe_shutdown,
}
