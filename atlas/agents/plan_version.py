"""Plan versioning + diff log.

The plan carries an identity across adaptations: same review_id,
incrementing version number, an audit diff-log of what changed.
The dashboard's Plan tab shows "Plan v3" with the history:

    v1 -> v2: skipped Vega (window expired during NO-GO 21:30-22:45)
    v2 -> v3: truncated session at 03:15 EDT (dewing forecast tightened)

Every adaptation is logged with timestamp, reason, and a structured
diff so the morning report can reconstruct the night.

Usage:

    from atlas.agents.plan_version import init_version, next_version

    # When the Planner first publishes a fresh plan:
    plan = init_version(plan, review_id="rev-2026-05-29-001",
                        reason="startup")

    # When the Planner re-publishes a *materially different* plan
    # (same identity, new content), use next_version with a diff:
    plan = next_version(prev_plan=old_plan, new_plan=new_plan,
                        reason="critic added moon advisory")

    # When an adaptation verb (pause / drop / truncate / ...) fires:
    plan = bump_for_adaptation(plan, verb="drop_slot",
                                reason="window expired",
                                diff={"dropped": ["Vega"]})

The plan dict keeps two new fields:
    version: int                       (starts at 1)
    version_history: list[dict]        (one entry per bump)

Each history entry:
    {
        "version": 2,
        "parent_version": 1,
        "at": "2026-05-29T01:30:00Z",
        "reason": "critic added moon advisory",
        "verb": "republish" | "pause" | "drop_slot" | ...,
        "diff": {"added": [...], "removed": [...], "changed": [...]},
    }
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any


HISTORY_MAX = 50            # cap to keep memory + UI bounded


# ---- Public API ------------------------------------------------------------


def init_version(plan: dict, *, review_id: str | None = None,
                   reason: str = "fresh plan") -> dict:
    """Initialise version=1 and an empty history.

    Safe to call on a plan that already has a version (it will reset
    to v1) — only call this on a *fresh* Planner output, not on an
    adaptation. Adaptations use bump_for_adaptation() instead.

    Returns the mutated plan (also mutates in place — convenience)."""
    if review_id is not None:
        plan["review_id"] = review_id
    plan["version"] = 1
    plan["version_history"] = [{
        "version": 1,
        "parent_version": None,
        "at": _now_iso(),
        "reason": reason,
        "verb": "create",
        "diff": {},
    }]
    return plan


def next_version(*, prev_plan: dict | None, new_plan: dict,
                   reason: str, verb: str = "republish") -> dict:
    """Bump the version when the Planner re-publishes a fresh plan.

    If ``prev_plan`` is None or has no version, this falls back to
    init_version() — the first publication starts at v1, no history
    bump needed. Otherwise the new_plan inherits prev_plan's
    review_id (identity is stable across adaptations) and gets
    version = prev_plan.version + 1.

    Returns the mutated new_plan."""
    if not prev_plan or not prev_plan.get("version"):
        return init_version(new_plan, reason=reason)

    prev_v = int(prev_plan.get("version") or 1)
    new_plan["review_id"] = prev_plan.get("review_id") or new_plan.get("review_id")
    new_plan["version"] = prev_v + 1

    diff = _structural_diff(prev_plan, new_plan)
    history = list(prev_plan.get("version_history") or [])
    history.append({
        "version": new_plan["version"],
        "parent_version": prev_v,
        "at": _now_iso(),
        "reason": reason,
        "verb": verb,
        "diff": diff,
    })
    new_plan["version_history"] = history[-HISTORY_MAX:]
    return new_plan


def bump_for_adaptation(plan: dict, *, verb: str, reason: str,
                          diff: dict | None = None) -> dict:
    """Bump the version for an in-session adaptation (one of the seven
    verbs: pause / resume / drop_slot / truncate / swap / insert /
    safe_shutdown). The plan is mutated in place; returns it for
    chaining.

    ``diff`` is a small structured payload describing what the verb
    changed (e.g. {"dropped": ["Vega"]}, {"truncated_at": "03:15Z"}).
    Free-form — the dashboard renders it verbatim alongside the
    reason."""
    if not plan.get("version"):
        plan["version"] = 1
        plan["version_history"] = []
    prev_v = int(plan["version"])
    plan["version"] = prev_v + 1
    history = list(plan.get("version_history") or [])
    history.append({
        "version": plan["version"],
        "parent_version": prev_v,
        "at": _now_iso(),
        "reason": reason,
        "verb": verb,
        "diff": diff or {},
    })
    plan["version_history"] = history[-HISTORY_MAX:]
    return plan


def format_history_line(entry: dict) -> str:
    """One-line rendering for logs + chat.

    Example: 'v1 -> v2: drop_slot Vega (window expired during NO-GO)'
    """
    pv = entry.get("parent_version")
    v = entry.get("version")
    verb = entry.get("verb") or "change"
    reason = entry.get("reason") or ""
    diff = entry.get("diff") or {}
    detail = ""
    if isinstance(diff, dict) and diff:
        # Render the first interesting key
        for k in ("dropped", "added", "swapped", "truncated_at",
                  "inserted", "paused_at", "resumed_at", "changed"):
            if k in diff:
                detail = f" [{k}: {diff[k]}]"
                break
    if pv is None:
        return f"v{v}: {verb} ({reason}){detail}"
    return f"v{pv} -> v{v}: {verb} ({reason}){detail}"


# ---- Internals -------------------------------------------------------------


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _structural_diff(prev: dict, curr: dict) -> dict:
    """Compute a small structured diff between two plans.

    We compare the visible_targets list by target_name + start_utc
    (the slot identity). Returns added/removed/changed lists.

    Cheap — operates on already-loaded dicts; no DB."""
    def _slot_key(t: dict) -> tuple:
        return (t.get("target_name") or "?",
                  t.get("start_utc") or "")

    prev_targets = prev.get("visible_targets") or []
    curr_targets = curr.get("visible_targets") or []

    prev_keys = {_slot_key(t): t for t in prev_targets}
    curr_keys = {_slot_key(t): t for t in curr_targets}

    added = [k[0] for k in curr_keys.keys() - prev_keys.keys()]
    removed = [k[0] for k in prev_keys.keys() - curr_keys.keys()]

    changed: list[str] = []
    for k in prev_keys.keys() & curr_keys.keys():
        pp, cc = prev_keys[k], curr_keys[k]
        if (pp.get("end_utc") != cc.get("end_utc") or
                pp.get("priority") != cc.get("priority") or
                pp.get("workflow") != cc.get("workflow")):
            changed.append(k[0])

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "prev_count": len(prev_targets),
        "curr_count": len(curr_targets),
    }
