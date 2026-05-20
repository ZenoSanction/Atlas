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


PLANNER_TOOLS: list[ToolSpec] = [
    ToolSpec("get_tonight_plan",
             "Get the Planner's current tonight plan: visible targets, "
             "active campaigns count, dark-window times, fallback status. "
             "This is the same data the dashboard's Plan tab shows.",
             {"type": "object", "properties": {}},
             _get_tonight_plan),
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
]
