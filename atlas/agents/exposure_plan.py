"""Default per-workflow / per-filter exposure plans.

The Planner currently lists visible targets with priorities — but no
guidance on what exposure each target should get. This module provides
sensible defaults per workflow that the Planner attaches to each plan
entry. NINA-XML generation (a later pass) consumes these.

Defaults are amateur-typical for a fast 8-10" optic at Bortle 4-5.
The operator can override per campaign in the campaign.success_criterion
JSON or via a future Setup-tab editor.
"""
from __future__ import annotations


# Per-workflow exposure templates. Each entry: filter -> (exposure_s, count).
# Total integration per filter ≈ exposure_s × count.
DEFAULT_EXPOSURE_PLANS = {
    "deepsky": {
        # Aesthetic LRGB + narrowband. Tuned for OSC users by default; mono
        # users can extend to per-filter sets.
        "OSC":  [("OSC", 180, 60)],         # ~3 h on OSC
        "MONO": [
            ("L",  180, 40),   # 2 h luminance
            ("R",  120, 25),
            ("G",  120, 25),
            ("B",  120, 25),
            ("Ha", 600, 12),   # 2 h Hα
        ],
    },
    "astrometry": {
        # Asteroid / comet — short exposures, lots of frames for centroid.
        "OSC":  [("OSC", 30, 10)],
        "MONO": [("L",   30, 10)],
    },
    "photometry": {
        # Variable star — long continuous series, NO dithering, single filter.
        "OSC":  [("OSC", 60, 120)],   # 2 h continuous
        "MONO": [("V",   60, 120)],   # Johnson V if available
    },
    "exoplanet": {
        # Transit photometry — fixed field, locked focus, sub-second timing.
        # ~3 h covering ingress + transit + egress.
        "OSC":  [("OSC", 60, 180)],
        "MONO": [("V",   60, 180)],
    },
    "transient": {
        # SN hunting — deep stack per visit, multiple filters helpful.
        "OSC":  [("OSC", 120, 30)],
        "MONO": [("L",   120, 30)],
    },
    "planetary": {
        # Lucky imaging — SER video. Stored as N "frames" each ~3s for
        # bookkeeping; the actual capture is SharpCap-driven.
        "OSC":  [("OSC", 3, 6000)],
        "MONO": [("L",   3, 6000)],
    },
}


def default_plan_for(workflow: str, camera_type: str = "OSC") -> list[dict]:
    """Return the default exposure plan for a workflow + camera type.

    Each entry: {"filter": "L", "exposure_s": 180, "count": 40,
                 "total_s": 7200, "total_min": 120}.
    """
    workflow = (workflow or "deepsky").lower()
    camera_type = (camera_type or "OSC").upper()
    table = DEFAULT_EXPOSURE_PLANS.get(workflow, DEFAULT_EXPOSURE_PLANS["deepsky"])
    series = table.get(camera_type) or table.get("OSC") or []
    out = []
    for filt, exp, count in series:
        total = exp * count
        out.append({
            "filter": filt,
            "exposure_s": exp,
            "count": count,
            "total_s": total,
            "total_min": round(total / 60.0, 1),
        })
    return out


def total_integration_min(plan: list[dict]) -> float:
    """Sum total integration in minutes across all filter sets in a plan."""
    return round(sum(p.get("total_min", 0.0) for p in plan), 1)
