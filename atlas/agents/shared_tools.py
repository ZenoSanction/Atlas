"""Shared tools registered on every agent.

These are tools all five agents need access to — most importantly,
the ability to READ the current session plan so any of them can
answer "what's on the plan tonight?" or "what did the Planner
produce?" without having to be the Planner.
"""
from __future__ import annotations

from typing import Any

from atlas.agents.base import ToolSpec


async def _get_current_plan(p: dict) -> dict:
    """Return the current tonight_plan + session_review (including
    every advisory from the review chain). Read-only.

    Every agent has this so the Critic, Operator, Oracle, and
    Archivist can answer plan-related questions from chat without
    needing the Planner-specific tools."""
    from atlas.agents.state import get_state
    st = get_state()
    plan = st.get_tonight_plan()
    review = st.get_session_review() or {}
    phase = st.get_review_phase()
    if plan is None:
        return {
            "plan_present": False,
            "message": ("No tonight plan in shared state yet. The "
                          "Planner builds one on startup and on every "
                          "rebuild — give it a moment. If this persists, "
                          "check /api/plan/diagnose."),
            "review_phase": phase.get("phase"),
        }
    return {
        "plan_present": True,
        "built_at": plan.get("built_at"),
        "reason": plan.get("reason"),
        "active_campaigns": plan.get("active_campaigns"),
        "visible_targets_count": len(plan.get("visible_targets") or []),
        "considered_count": plan.get("considered_count"),
        "scheduled_total_min": plan.get("scheduled_total_min"),
        "dark_window_min": plan.get("dark_window_min"),
        "fallback_to_catalog": plan.get("fallback_to_catalog"),
        "blocked_reason": plan.get("blocked_reason"),
        "visible_targets": [
            {
                "i": i + 1,
                "target_name": t.get("target_name"),
                "campaign_name": t.get("campaign_name"),
                "workflow": t.get("workflow"),
                "ra_deg": t.get("ra_deg"),
                "dec_deg": t.get("dec_deg"),
                "priority": t.get("priority"),
                "start_utc": t.get("start_utc"),
                "end_utc": t.get("end_utc"),
                "scheduled_for_min": t.get("scheduled_for_min"),
                "peak_alt_deg": t.get("peak_alt_deg"),
            }
            for i, t in enumerate(plan.get("visible_targets") or [])
        ],
        "advisories": [
            {
                "kind": a.get("kind"),
                "severity": a.get("severity"),
                "source": a.get("source"),
                "message": a.get("message"),
                "at": a.get("at"),
            }
            for a in (review.get("advisories") or [])
        ],
        "advisory_count": len(review.get("advisories") or []),
        "review_state": review.get("state"),
        "review_phase": phase.get("phase"),
        "review_phase_updated_at": phase.get("updated_at"),
    }


GET_CURRENT_PLAN_TOOL = ToolSpec(
    "get_current_plan",
    "Read the Planner's current tonight plan + all advisories from "
    "the review chain (Critic, Operator, Oracle). Returns: targets "
    "with start/end times + priorities + RA/Dec, advisories with "
    "severities + sources, review_phase (which stage of the chain "
    "is active), and key meta (built_at, campaigns count, dark "
    "window minutes). Call this BEFORE saying you can't see the "
    "plan — every agent has this tool.",
    {"type": "object", "properties": {}},
    _get_current_plan,
)


def all_shared_tools() -> list[ToolSpec]:
    """Tools that every agent gets registered automatically."""
    return [GET_CURRENT_PLAN_TOOL]
