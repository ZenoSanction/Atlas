"""Tools the Planner agent can use when chatted with.

The Planner thinks in terms of nightly schedules, target visibility, the
seasonal sky, and active campaigns. Tools below let it answer questions
like "what's on tonight?", "is M42 up?", "what are the best targets in
May from a +29 N site?", and "rebuild the plan now".
"""
from __future__ import annotations

from datetime import datetime

from atlas.agents.base import ToolSpec
from atlas.agents.state import get_state
from atlas.astronomy import compute_alt_az, airmass, night_window
from atlas.astronomy.catalog import best_now, all_entries
from atlas.db.managers import CampaignManager, ConfigManager


async def _get_tonight_plan(_p: dict) -> dict:
    plan = get_state().get_tonight_plan()
    if plan is None:
        return {"plan": None,
                "message": "No plan computed yet — agent just started or no site config."}
    return {"plan": plan}


async def _get_night_window(_p: dict) -> dict:
    site = ConfigManager.get_site()
    if site is None:
        return {"error": "Site coordinates not configured. Open Setup."}
    nw = night_window(float(site.latitude), float(site.longitude),
                      datetime.utcnow(), altitude_deg=-12.0)
    if nw is None:
        return {"window": None,
                "message": "No dark window found in the next 36 hours (polar day?)."}
    dusk, dawn = nw
    return {
        "dusk_utc": dusk.isoformat(timespec="seconds") + "Z",
        "dawn_utc": dawn.isoformat(timespec="seconds") + "Z",
        "hours": round((dawn - dusk).total_seconds() / 3600, 2),
        "twilight": "nautical_-12",
    }


async def _check_target_visibility(p: dict) -> dict:
    ra = float(p["ra_deg"])
    dec = float(p["dec_deg"])
    site = ConfigManager.get_site()
    if site is None:
        return {"error": "Site coordinates not configured."}
    now = datetime.utcnow()
    alt, az = compute_alt_az(ra, dec, float(site.latitude),
                              float(site.longitude), now)
    return {
        "ra_deg": ra, "dec_deg": dec,
        "alt_deg": round(alt, 2),
        "az_deg": round(az, 2),
        "airmass": (round(airmass(alt), 2) if airmass(alt) is not None else None),
        "above_horizon": alt > 0,
        "above_site_horizon": alt >= float(site.horizon_alt_min),
        "horizon_alt_min_deg": float(site.horizon_alt_min),
        "at_utc": now.isoformat(timespec="seconds") + "Z",
    }


async def _list_active_campaigns(_p: dict) -> dict:
    rows = CampaignManager.list_active()
    return {
        "count": len(rows),
        "campaigns": [
            {"id": c.id, "name": c.name,
              "workflow": c.workflow.value if hasattr(c.workflow, "value") else str(c.workflow),
              "priority": c.priority,
              "scientific_context": c.scientific_context}
            for c in rows
        ],
    }


async def _seasonal_showcase(p: dict) -> dict:
    month = int(p.get("month") or datetime.utcnow().month)
    limit = int(p.get("limit", 12))
    entries = best_now(month=month, limit=limit)
    return {"month": month, "count": len(entries), "entries": entries}


async def _rebuild_plan(p: dict) -> dict:
    """Trigger the Planner to (re)build the plan right now and publish it
    to the Plan tab. This is the *write* counterpart to get_tonight_plan."""
    reason = (p.get("reason") or "operator_chat_request").strip() or "operator_chat_request"
    try:
        from atlas.agents.coordinator import get_coordinator
        from atlas.db.models import AgentName
        planner = get_coordinator().get(AgentName.PLANNER)
    except Exception as e:
        return {"error": f"Planner agent not available: {e}"}
    # Drive the same rebuild path the periodic loop uses; identical
    # bulletproofing applies (any exception still publishes a
    # fallback empty plan with the error as an advisory).
    try:
        await planner._rebuild_plan(reason=reason)
    except Exception as e:
        try:
            await planner._publish_empty_plan_with_advisory(
                reason=f"chat_tool_failed:{reason}",
                advisory_kind="planner_error",
                advisory_severity="critical",
                advisory_msg=(f"Chat-tool-triggered rebuild crashed: "
                                f"{type(e).__name__}: {e}. Fallback empty "
                                f"plan published so the Plan tab is never empty."),
            )
        except Exception:
            return {"ok": False,
                      "error": f"Rebuild crashed AND fallback publish "
                                  f"failed: {type(e).__name__}: {e}"}
        return {"ok": False, "error": f"Rebuild crashed: {type(e).__name__}: {e}",
                  "fallback_published": True}
    # Read back what got published so the LLM can summarize it
    from atlas.agents.state import get_state
    plan = get_state().get_tonight_plan() or {}
    review = get_state().get_session_review() or {}
    return {
        "ok": True,
        "rebuilt": True,
        "reason_used": reason,
        "plan_built_at": plan.get("built_at"),
        "active_campaigns": plan.get("active_campaigns"),
        "visible_targets": len(plan.get("visible_targets") or []),
        "considered_count": plan.get("considered_count"),
        "review_state": review.get("state"),
        "advisory_count": len(review.get("advisories") or []),
        "message": ("Plan rebuilt and published to the Plan tab. "
                      "Dashboard's Plan tab will reflect this immediately."),
    }


async def _build_single_target_session(p: dict) -> dict:
    """Tool: dedicate the entire dark window to one target.

    Resolves the target name via SIMBAD then the local catalog (or
    trusts caller-supplied RA/Dec), computes its visibility inside
    tonight's astronomical dark window at the site horizon, and
    publishes a one-slot plan that auto-starts the review chain to
    the Critic. Same publish + chain machinery as rebuild_plan.
    """
    target_name = (p.get("target_name") or "").strip()
    if not target_name and (p.get("ra_deg") is None or p.get("dec_deg") is None):
        return {"error": "Provide target_name OR (ra_deg AND dec_deg)."}
    ra_deg = p.get("ra_deg")
    dec_deg = p.get("dec_deg")
    workflow = (p.get("workflow") or "deepsky").strip() or "deepsky"
    reason = (p.get("reason") or "single_target_chat_request").strip()
    try:
        from atlas.agents.coordinator import get_coordinator
        from atlas.db.models import AgentName
        planner = get_coordinator().get(AgentName.PLANNER)
    except Exception as e:
        return {"error": f"Planner agent not available: {e}"}
    try:
        return await planner.build_single_target_session(
            target_name=target_name,
            ra_deg=float(ra_deg) if ra_deg is not None else None,
            dec_deg=float(dec_deg) if dec_deg is not None else None,
            workflow=workflow,
            reason=reason,
        )
    except Exception as e:
        # Bulletproof: still try to publish something so the Plan tab
        # never sits empty. The Planner's _publish_empty_plan_with_advisory
        # is the last-resort safety net.
        try:
            await planner._publish_empty_plan_with_advisory(
                reason=f"single_target_crashed:{target_name}",
                advisory_kind="planner_error",
                advisory_severity="critical",
                advisory_msg=(
                    f"build_single_target_session crashed: "
                    f"{type(e).__name__}: {e}. Fallback empty plan "
                    f"published so the Plan tab is never empty."
                ),
            )
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                  "fallback_published": True}


async def _cancel_session(p: dict) -> dict:
    """Look up the Planner agent at call time so this tool can live in the
    module-level PLANNER_TOOLS list (matching the other tools' pattern)
    without needing the instance at import time."""
    reason = (p.get("reason") or "").strip()
    if not reason:
        return {"error": "reason is required for the audit trail."}
    try:
        from atlas.agents.coordinator import get_coordinator
        from atlas.db.models import AgentName
        planner = get_coordinator().get(AgentName.PLANNER)
    except Exception as e:
        return {"error": f"Planner agent not available: {e}"}
    try:
        await planner._cancel_session(reason=reason)
    except Exception as e:
        return {"error": f"Cancellation failed: {e}"}
    return {"ok": True, "cancelled": True, "reason": reason,
            "message": "Session cancelled. The current review is now terminal-cancelled."}


PLANNER_TOOLS: list[ToolSpec] = [
    ToolSpec("get_tonight_plan",
             "Get the Planner's current tonight plan: visible targets, "
             "active campaigns count, dark-window times, fallback status. "
             "This is the same data the dashboard's Plan tab shows. "
             "READ-ONLY — use rebuild_plan to actually (re)build and "
             "publish a plan to the Plan tab.",
             {"type": "object", "properties": {}},
             _get_tonight_plan),
    ToolSpec("rebuild_plan",
             "Rebuild the nightly plan NOW and publish it to the Plan tab. "
             "This is the write counterpart to get_tonight_plan. Use when "
             "the operator asks you to refresh / regenerate / publish a "
             "plan, when campaigns have changed, or when you want to "
             "incorporate new revisit candidates. ALWAYS publishes a plan "
             "even on error (fallback empty plan with the error as an "
             "advisory). Returns the rebuilt plan's summary stats. "
             "NOTE: this builds a normal multi-target plan from active "
             "campaigns. If the operator wants the WHOLE NIGHT dedicated "
             "to one target (\"dedicate tonight to NGC 7000\"), use "
             "build_single_target_session instead.",
             {"type": "object",
              "properties": {
                  "reason": {"type": "string",
                              "description": "Short audit reason for this rebuild (e.g. 'operator chat request', 'new campaign added'). Optional."},
              }},
             _rebuild_plan),
    ToolSpec("build_single_target_session",
             "Dedicate the ENTIRE astronomical dark window to one target. "
             "Use when the operator asks for a single-target session "
             "(\"dedicate tonight to NGC 7000\", \"all night on M42\", "
             "\"deep integration on the Orion Nebula\"). Resolves the name "
             "via SIMBAD then the local catalog; or pass ra_deg + dec_deg "
             "directly to skip the lookup. Builds a one-slot plan covering "
             "the longest contiguous visibility span inside [dusk, dawn] "
             "at the site horizon, publishes it as Plan v(N+1), and "
             "auto-starts the review chain to the Critic. Returns the "
             "resolved coords, slot minutes, and review_id. ALWAYS "
             "publishes a plan even on error.",
             {"type": "object",
              "properties": {
                  "target_name": {"type": "string",
                                    "description": "Object name to look up "
                                    "(SIMBAD/catalog). Required unless ra_deg+dec_deg given."},
                  "ra_deg": {"type": "number",
                              "description": "J2000 RA in degrees (0-360). "
                              "Optional; skips name resolution when paired with dec_deg."},
                  "dec_deg": {"type": "number",
                                "description": "J2000 Dec in degrees (-90 to +90)."},
                  "workflow": {"type": "string",
                                "description": "Workflow to apply: deepsky, "
                                "photometry, exoplanet, transient, planetary, "
                                "astrometry. Defaults to deepsky."},
                  "reason": {"type": "string",
                              "description": "Short audit reason. Optional."},
              }},
             _build_single_target_session),
    ToolSpec("get_night_window",
             "Get tonight's astronomical dark window (sun below -12°) at "
             "the configured site. Returns dusk_utc, dawn_utc, and hours.",
             {"type": "object", "properties": {}},
             _get_night_window),
    ToolSpec("check_target_visibility",
             "Compute current altitude/azimuth/airmass at the configured "
             "site for an arbitrary J2000 RA/Dec.",
             {"type": "object",
              "properties": {
                  "ra_deg": {"type": "number", "description": "RA in degrees (0-360)"},
                  "dec_deg": {"type": "number", "description": "Declination in degrees (-90 to +90)"},
              },
              "required": ["ra_deg", "dec_deg"]},
             _check_target_visibility),
    ToolSpec("list_active_campaigns",
             "List active campaigns ATLAS is tracking (id, name, workflow, "
             "priority, scientific context).",
             {"type": "object", "properties": {}},
             _list_active_campaigns),
    ToolSpec("seasonal_showcase",
             "Return the best showcase deep-sky objects from the built-in "
             "catalog for a given month (defaults to current month), "
             "sorted brightest-first. Used as fallback when no campaigns "
             "are active.",
             {"type": "object",
              "properties": {
                  "month": {"type": "integer", "minimum": 1, "maximum": 12},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 50},
              }},
             _seasonal_showcase),
    ToolSpec("cancel_session",
             "End the current session-planning workflow. Use when no viable "
             "plan is possible: operator said 'don't bother tonight', a "
             "re-plan with constraints would leave zero targets, or "
             "conditions are obviously hopeless before the Critic has run. "
             "Cancellation marks the live SessionReview terminal-cancelled "
             "and broadcasts; the dashboard shows it as the red end-state. "
             "Reason is mandatory for the audit trail.",
             {"type": "object",
              "properties": {
                  "reason": {"type": "string",
                              "description": "One-line audit reason."},
              },
              "required": ["reason"]},
             _cancel_session),
]
