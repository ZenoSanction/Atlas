"""Build a one-slot plan dedicating the entire dark window to one target.

Doctrine: when the operator chats "dedicate tonight to NGC 7000," the
generic _rebuild_plan walks active campaigns and produces a normal
multi-target queue — not what the operator asked for. This module
provides the alternate path:

  1. Resolve the target's coordinates (caller can pass RA/Dec, or we
     look it up via SIMBAD / the on-disk catalog).
  2. Compute the target's visibility inside tonight's astronomical
     dark window at the configured site horizon.
  3. Build a single slot covering the intersection (visible_from,
     visible_until) clipped to [dusk, dawn], with the same shape the
     normal scheduler emits.
  4. Publish the plan with versioning + auto-start the review chain
     to the Critic exactly like _rebuild_plan does.

Exposed as:
  - Planner.build_single_target_session(target_name, ...) async method
  - PLANNER_TOOL "build_single_target_session" (so chatting with the
    Planner directly works)
  - OPERATOR_TOOL "build_single_target_session" on ATLAS (so operator
    chat with ATLAS produces the plan without an extra hop)

Both tool surfaces call the same Planner method, which is the single
source of truth.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional


async def resolve_target(name: str, *,
                            ra_deg: float | None = None,
                            dec_deg: float | None = None,
                            ) -> dict | None:
    """Return a minimal target dict with target_name + ra_deg + dec_deg.

    Resolution order:
      1. If RA/Dec were passed in, trust them and skip lookup.
      2. SIMBAD TAP query (network).
      3. Local catalog (catalogs/ directory).

    Returns None when nothing resolves — the caller should publish an
    empty plan with a "couldn't find X" advisory rather than just hang.
    """
    nm = (name or "").strip()
    if ra_deg is not None and dec_deg is not None:
        return {
            "target_name": nm or "Unknown",
            "ra_deg": float(ra_deg),
            "dec_deg": float(dec_deg),
            "source": "caller_provided",
        }
    if not nm:
        return None
    # SIMBAD first (most authoritative for named DSOs)
    try:
        from atlas.astronomy.simbad import resolve as simbad_resolve
        hit = await simbad_resolve(nm)
        if hit:
            return {
                "target_name": hit.main_id or nm,
                "ra_deg": hit.ra_deg,
                "dec_deg": hit.dec_deg,
                "object_type": hit.object_type,
                "magnitude": hit.magnitude,
                "source": "simbad",
            }
    except Exception:
        pass
    # Local catalog fallback — the catalog returns ranked matches
    # (exact name first, then prefix, then substring). Take the first
    # hit; the operator can override by specifying RA/Dec if it's wrong.
    try:
        from atlas.astronomy.catalog import search as catalog_search
        hits = catalog_search(nm, limit=1)
        if hits:
            cat = hits[0]
            return {
                "target_name": cat.get("name") or nm,
                "ra_deg": float(cat["ra_deg"]),
                "dec_deg": float(cat["dec_deg"]),
                "object_type": cat.get("object_type"),
                "magnitude": cat.get("magnitude"),
                "source": "catalog",
            }
    except Exception:
        pass
    return None


def build_single_target_plan(*, target: dict, lat: float, lon: float,
                               horizon_alt: float = 25.0,
                               reference_utc: datetime | None = None,
                               workflow: str = "deepsky",
                               reason: str = "single_target_chat_request",
                               ) -> dict:
    """Compute the night window + visibility for one target and return
    a plan dict ready to publish.

    Returns a fully-shaped tonight_plan with exactly one slot covering
    the longest contiguous visibility span inside [dusk, dawn]. If the
    target never clears the horizon, returns an empty-targets plan
    carrying a blocked_reason explaining why."""
    from atlas.astronomy.visibility import night_window
    from atlas.astronomy.scheduler import compute_visibility_window
    from atlas.astronomy.day_phase import current_phase

    if reference_utc is None:
        reference_utc = datetime.utcnow()
    now_str = reference_utc.isoformat(timespec="seconds") + "Z"

    # Use astronomical dark (-18°) as the imaging window — same
    # boundary the operator means when they say "the whole night."
    nw = night_window(lat, lon, reference_utc=reference_utc,
                        altitude_deg=-18.0)
    if not nw:
        return _empty_plan(
            target_name=target.get("target_name"),
            reason=reason,
            now=now_str,
            blocked_reason="No astronomical dark window in next 36 h "
                           "at this site (polar day?).",
            day_phase=current_phase(lat, lon, reference_utc),
            horizon_alt=horizon_alt,
        )
    dusk, dawn = nw
    dark_min = (dawn - dusk).total_seconds() / 60.0

    visibility = compute_visibility_window(
        target, lat, lon, horizon_alt, dusk, dawn,
    )
    if visibility is None:
        return _empty_plan(
            target_name=target.get("target_name"),
            reason=reason,
            now=now_str,
            blocked_reason=(
                f"{target.get('target_name')!r} never clears "
                f"{horizon_alt:.0f}° tonight (dusk {dusk.isoformat()}Z "
                f"to dawn {dawn.isoformat()}Z)."
            ),
            day_phase=current_phase(lat, lon, reference_utc),
            horizon_alt=horizon_alt,
            window=(dusk, dawn),
        )

    slot_start = visibility.visible_from
    slot_end = visibility.visible_until
    slot_min = (slot_end - slot_start).total_seconds() / 60.0
    peak_alt = visibility.peak_alt_deg

    slot = {
        "target_name": target.get("target_name"),
        "workflow": workflow,
        "ra_deg": target["ra_deg"],
        "dec_deg": target["dec_deg"],
        "object_type": target.get("object_type"),
        "magnitude": target.get("magnitude"),
        "priority": 1,
        "campaign_id": None,
        "campaign_name": "single_target_chat",
        "start_utc": slot_start.isoformat(timespec="seconds") + "Z",
        "end_utc":   slot_end.isoformat(timespec="seconds") + "Z",
        "scheduled_for_min": round(slot_min, 1),
        "peak_alt_deg": peak_alt,
        "preferred_dwell_min": round(slot_min, 1),
        "single_target_dedicated": True,
    }

    day_phase = current_phase(lat, lon, reference_utc)
    plan = {
        "built_at": now_str,
        "reason": reason,
        "active_campaigns": 0,
        "visible_targets": [slot],
        "considered": [slot],
        "considered_count": 1,
        "unscheduled": [],
        "max_targets_per_session": 1,
        "min_dwell_minutes": round(slot_min, 1),
        "scheduled_total_min": round(slot_min, 1),
        "dark_window_min": round(dark_min, 1),
        "overruns_dark_window": False,
        "fit_strategy": "single_target",
        "skipped_below_horizon": 0,
        "skipped_no_coords": 0,
        "horizon_alt_min_deg": horizon_alt,
        "window": {
            "dusk_utc": dusk.isoformat(timespec="seconds") + "Z",
            "dawn_utc": dawn.isoformat(timespec="seconds") + "Z",
        },
        "day_phase": day_phase.to_jsonable(),
        "fallback_to_catalog": False,
        "applied_constraints": ["single_target_chat_request"],
    }
    return plan


def _empty_plan(*, target_name: str | None, reason: str, now: str,
                  blocked_reason: str, day_phase, horizon_alt: float,
                  window: tuple | None = None) -> dict:
    """Build the empty-but-shaped plan returned when we can't build a
    real slot (no dark window, never above horizon, etc.). Keeps the
    Plan tab populated with the explanation rather than going blank."""
    win_payload = None
    if window:
        dusk, dawn = window
        win_payload = {
            "dusk_utc": dusk.isoformat(timespec="seconds") + "Z",
            "dawn_utc": dawn.isoformat(timespec="seconds") + "Z",
        }
    return {
        "built_at": now,
        "reason": reason,
        "active_campaigns": 0,
        "visible_targets": [],
        "considered": [],
        "considered_count": 0,
        "unscheduled": [],
        "max_targets_per_session": 1,
        "min_dwell_minutes": 0.0,
        "scheduled_total_min": 0.0,
        "dark_window_min": None,
        "overruns_dark_window": False,
        "fit_strategy": "single_target",
        "skipped_below_horizon": 1 if target_name else 0,
        "skipped_no_coords": 0,
        "horizon_alt_min_deg": horizon_alt,
        "window": win_payload,
        "day_phase": day_phase.to_jsonable(),
        "fallback_to_catalog": False,
        "applied_constraints": ["single_target_chat_request"],
        "blocked_reason": blocked_reason,
    }
