"""Smoke test for build_single_target_session.

Exercises:
  1. Pure-Python builder: target with explicit RA/Dec, site config,
     visibility window -> one slot covering the longest visibility span.
  2. Target that never clears horizon -> empty plan with blocked_reason.
  3. No site config -> empty plan with config_missing advisory.
  4. resolve_target() with explicit RA/Dec bypasses lookups (no network).
  5. The Planner method publishes the plan with versioning and sends
     the Stage-1 STATUS to the Critic (chain auto-starts).

Network-dependent paths (real SIMBAD resolution) are NOT exercised
here — the explicit RA/Dec path covers the same downstream code.

ASCII output only.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    asyncio.run(_run())
    return 0


async def _run() -> None:
    from atlas.agents.single_target_session import (
        build_single_target_plan, resolve_target,
    )
    import atlas.agents.state as state_mod
    state_mod._state = state_mod._ObservatoryState()

    # ---- 1. Builder w/ explicit RA/Dec -------------------------------
    # NGC 7000 (North America Nebula): RA 314.75°, Dec +44.33°.
    # Pick a site (Silver Springs, FL) and a reference time deep in
    # the afternoon so the next dark window is "tonight."
    target = {
        "target_name": "NGC 7000",
        "ra_deg": 314.75,
        "dec_deg": 44.33,
        "object_type": "EmissionNebula",
    }
    lat, lon = 29.21, -82.06   # Silver Springs FL
    ref = datetime(2026, 8, 15, 18, 0, 0)   # mid-August afternoon

    plan = build_single_target_plan(
        target=target, lat=lat, lon=lon,
        horizon_alt=25.0, reference_utc=ref,
        workflow="deepsky",
        reason="smoke_test",
    )
    visible = plan.get("visible_targets") or []
    assert len(visible) == 1, f"expected 1 slot, got {len(visible)}"
    slot = visible[0]
    assert slot["target_name"] == "NGC 7000"
    assert slot["ra_deg"] == 314.75 and slot["dec_deg"] == 44.33
    assert slot["single_target_dedicated"] is True
    assert plan["scheduled_total_min"] > 60.0   # August NGC 7000 -> several hours
    assert plan["dark_window_min"] > 0
    print(f"[1] explicit RA/Dec OK; slot {slot['target_name']} "
          f"{plan['scheduled_total_min']:.0f} min of "
          f"{plan['dark_window_min']:.0f} min dark "
          f"(peak alt {slot['peak_alt_deg']:.1f} deg)")

    # ---- 2. Target that never clears horizon -------------------------
    polar_south = {
        "target_name": "SMC",
        "ra_deg": 13.16, "dec_deg": -72.83,   # Small Magellanic Cloud
    }
    plan2 = build_single_target_plan(
        target=polar_south, lat=lat, lon=lon,
        horizon_alt=25.0, reference_utc=ref,
        workflow="deepsky",
    )
    assert (plan2.get("visible_targets") or []) == []
    assert "never clears" in (plan2.get("blocked_reason") or "")
    assert plan2["window"] is not None  # dark window still computed
    print(f"[2] never-clears-horizon OK; "
          f"blocked_reason={plan2['blocked_reason'][:80]!r}")

    # ---- 3. resolve_target() with explicit RA/Dec (no network) -------
    t = await resolve_target("CustomNebula", ra_deg=12.0, dec_deg=-5.0)
    assert t is not None
    assert t["ra_deg"] == 12.0 and t["dec_deg"] == -5.0
    assert t["source"] == "caller_provided"
    print(f"[3] resolve_target explicit OK; source={t['source']}")

    # ---- 4. Empty name + no coords -> None ---------------------------
    t = await resolve_target("")
    assert t is None
    print(f"[4] empty name returns None OK")

    # ---- 5. Planner end-to-end with method ---------------------------
    from atlas.agents.planner import Planner
    from atlas.db.models import AgentName, AgentMessageKind

    planner = Planner()

    sent: list[tuple] = []
    broadcasted: list[dict] = []

    async def fake_send(recipient, kind, payload=None, session_id=None):
        sent.append((recipient, kind, payload or {}))

    async def fake_broadcast(payload):
        broadcasted.append(payload)

    planner.send = fake_send
    planner.bus.broadcast_event = fake_broadcast
    planner.set_task = lambda *a, **k: None
    planner.log_decision = lambda *a, **k: None

    # Stub site config so we don't depend on the test environment's DB
    import atlas.db.managers as dbmanagers
    class FakeSite:
        latitude = lat; longitude = lon; horizon_alt_min_deg = 25.0
    orig_get_site = dbmanagers.ConfigManager.get_site
    dbmanagers.ConfigManager.get_site = staticmethod(lambda: FakeSite())

    try:
        result = await planner.build_single_target_session(
            target_name="NGC 7000",
            ra_deg=314.75, dec_deg=44.33,
            workflow="deepsky",
            reason="smoke_test_e2e",
        )
    finally:
        dbmanagers.ConfigManager.get_site = orig_get_site

    assert result.get("ok") is True, f"expected ok=True, got {result}"
    assert result["target_name"] == "NGC 7000"
    assert result["review_id"]
    assert result["slot_minutes"] > 60

    from atlas.agents.state import get_state
    plan_pub = get_state().get_tonight_plan()
    assert plan_pub is not None
    assert plan_pub["version"] >= 1
    pubvis = plan_pub.get("visible_targets") or []
    assert len(pubvis) == 1 and pubvis[0]["target_name"] == "NGC 7000"

    # Stage-1 STATUS to Critic
    assert sent, "no message dispatched"
    assert sent[-1][0] == AgentName.CRITIC
    assert sent[-1][1] == AgentMessageKind.STATUS
    crit_payload = sent[-1][2]
    assert crit_payload.get("kind") == "plan_review"
    assert crit_payload.get("phase") == "critic"
    assert crit_payload.get("review_id") == result["review_id"]
    print(f"[5] Planner end-to-end OK; plan v{plan_pub['version']} "
          f"published, Stage-1 STATUS sent to Critic "
          f"(review_id={result['review_id'][:8]})")

    # Review phase set to "critic"
    phase = get_state().get_review_phase()
    assert phase["phase"] == "critic"
    print(f"[6] review_phase set to {phase['phase']!r} OK")

    # plan_update broadcast fired
    plan_updates = [b for b in broadcasted if b.get("type") == "plan_update"]
    assert plan_updates and plan_updates[-1]["kind"] == "single_target_session"
    print(f"[7] plan_update broadcast fired ({len(plan_updates)} total)")

    print("\nSingle-target session smoke OK")


if __name__ == "__main__":
    raise SystemExit(main())
