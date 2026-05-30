"""End-to-end smoke for the doctrine wiring.

Exercises the full path:
  RightNow snapshot -> Confidence Layer 1 rule -> narrator deliberation
  -> adapt_plan -> plan version bumps + execution snapshot updates.

Also verifies the slot-executor execution-snapshot updates happen at
the right moments (active_slot set, then cleared) via a minimal stub.

Run:
    python scripts/smoke_doctrine_wiring.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    asyncio.run(_run())
    return 0


async def _run():
    from atlas.agents.state import (
        get_state, OperatorVerdict, WeatherAssessment, MetricCheck,
        ExecutionSnapshot, _ObservatoryState,
    )
    import atlas.agents.state as state_mod
    # Reset singleton between runs so we start clean
    state_mod._state = _ObservatoryState()
    from atlas.agents.plan_version import init_version
    from atlas.agents.plan_adapter import adapt_plan
    from atlas.agents.confidence import recommend, safe_default_when_unresolved
    from atlas.agents.narrator import deliberate, resolve, RESOLUTION_APPLIED

    st = get_state()

    # ---- 1. Build a fresh versioned plan ---------------------------
    plan = init_version({
        "review_id": "rev-test-1",
        "visible_targets": [
            {"target_name": "M51", "workflow": "deepsky",
             "start_utc": "2026-05-29T02:00:00Z",
             "end_utc": "2026-05-29T03:30:00Z", "priority": 1},
            {"target_name": "M13", "workflow": "deepsky",
             "start_utc": "2026-05-29T03:30:00Z",
             "end_utc": "2026-05-29T05:00:00Z", "priority": 2},
        ],
        "active_campaigns": 1,
        "scheduled_total_min": 180,
        "dark_window_min": 300,
    }, review_id="rev-test-1", reason="startup")
    st.set_tonight_plan(plan)
    print(f"[1] plan v{plan['version']} published with {len(plan['visible_targets'])} slots")

    # ---- 2. Confidence Layer 1 unresolved on a quiet snapshot ------
    st.set_verdict(OperatorVerdict(decided_at="2026-05-29T01:00:00Z",
                                    verdict="GO", reason="clear"))
    st.set_assessment(WeatherAssessment(
        observed_at="t", assessed_at="t",
        overall_severity="ok", summary="clear, calm",
    ))
    rn = st.get_right_now()
    rec = recommend(rn)
    assert rec is None, f"expected UNRESOLVED, got {rec}"
    print("[2] quiet snapshot -> Confidence Layer 1 UNRESOLVED (correct)")

    # ---- 3. Simulate critical weather -> Layer 1 fires safe_shutdown ----
    st.set_assessment(WeatherAssessment(
        observed_at="t2", assessed_at="t2",
        overall_severity="critical",
        summary="Wind gust 45 mph, storm cell W",
    ))
    rn = st.get_right_now()
    rec = recommend(rn)
    assert rec is not None and rec.verb == "safe_shutdown", f"got {rec}"
    print(f"[3] critical weather -> rule={rec.rule_name}, verb={rec.verb}")

    # ---- 4. Deliberation: timeout-applied path runs the verb -------
    res = await deliberate(
        verb=rec.verb,
        reason=rec.reason,
        narration=f"Considering: {rec.verb}. {rec.reason}.",
        evidence=rec.evidence,
        decide_after_s=0.2,
        default_action="apply",
        confidence_layer=rec.confidence_layer,
        severity=rec.severity,
    )
    assert res.resolution == RESOLUTION_APPLIED
    assert res.adaptation.ok
    print(f"[4] narrator timeout-apply -> {res.adaptation.summary}")

    plan_now = st.get_tonight_plan()
    print(f"    plan version after deliberation: v{plan_now['version']}")
    assert plan_now["version"] >= 2
    exec_snap = st.get_execution()
    assert "safe-shutdown" in (exec_snap.blocked_reason or ""), exec_snap.blocked_reason
    print(f"    execution.blocked_reason: {exec_snap.blocked_reason!r}")

    # ---- 5. Reset; verify slot-executor-style snapshot updates -----
    # Mirror what _walk_plan_slots_inner does, just enough to exercise
    # the update_execution / clear_execution surfaces.
    st.set_tonight_plan(init_version({
        "review_id": "rev-test-2",
        "visible_targets": [
            {"target_name": "M51", "workflow": "deepsky",
             "start_utc": "2026-05-29T02:00:00Z",
             "end_utc": "2026-05-29T03:30:00Z", "ra_deg": 202.5, "dec_deg": 47.2},
        ],
    }, review_id="rev-test-2", reason="reset"))
    st.set_assessment(WeatherAssessment(
        observed_at="t3", assessed_at="t3",
        overall_severity="ok", summary="clear",
    ))
    st.clear_execution()
    assert st.get_execution().active_slot is None

    slots = (st.get_tonight_plan() or {}).get("visible_targets") or []
    last_end = slots[-1].get("end_utc")
    st.update_execution(planned_session_end=last_end)
    slot = slots[0]
    st.update_execution(
        active_slot={
            "target_name": slot.get("target_name"),
            "workflow": slot.get("workflow"),
            "start_utc": slot.get("start_utc"),
            "end_utc": slot.get("end_utc"),
        },
        active_action=f"capturing (1/{len(slots)}) {slot['target_name']}",
        next_action="end of session",
    )
    rn = st.get_right_now()
    assert rn["procedural"]["active_slot"]["target_name"] == "M51"
    assert "capturing" in rn["procedural"]["active_action"]
    assert rn["situational"]["session_active"] is True
    assert rn["procedural"]["planned_session_end"] == last_end
    print(f"[5] slot-executor surfaces populated; summary: {rn['summary']!r}")

    # ---- 6. Clean-up clears the snapshot ---------------------------
    st.clear_execution()
    rn = st.get_right_now()
    assert rn["procedural"]["active_slot"] is None
    assert rn["situational"]["session_active"] is False
    print(f"[6] post-walk clear OK; summary: {rn['summary']!r}")

    # ---- 7. Operator override path -------------------------------
    st.set_assessment(WeatherAssessment(
        observed_at="t4", assessed_at="t4",
        overall_severity="ok", summary="clear",
    ))
    # Make active_window_expired fire by setting an expired active_slot
    from datetime import datetime, timedelta
    past = (datetime.utcnow() - timedelta(hours=2)).isoformat(timespec="seconds") + "Z"
    st.update_execution(
        active_slot={"target_name": "Vega", "end_utc": past, "workflow": "photometry"},
    )
    # Also need plan to have a Vega slot so adapt_plan drop_slot works
    st.set_tonight_plan(init_version({
        "visible_targets": [
            {"target_name": "Vega", "start_utc": "t", "end_utc": past, "workflow": "photometry"},
            {"target_name": "M27", "start_utc": "t2", "end_utc": "t3", "workflow": "deepsky"},
        ],
    }, review_id="rev-test-3", reason="for override test"))

    rn = st.get_right_now()
    rec = recommend(rn)
    assert rec is not None and rec.verb == "drop_slot", f"got {rec}"
    print(f"[7] active_window_expired rule -> verb={rec.verb}, "
          f"target={rec.verb_kwargs.get('target_name')}")

    # Operator overrides with a different verb: truncate instead
    from atlas.agents.narrator import active_decisions
    async def overrider():
        await asyncio.sleep(0.05)
        ids = active_decisions()
        if ids:
            await resolve(ids[0], action="override",
                            verb="truncate",
                            reason="operator: just end session",
                            kwargs={"after_slot": "Vega"})
    asyncio.create_task(overrider())
    res = await deliberate(
        verb=rec.verb, reason=rec.reason,
        narration="Considering drop_slot.",
        evidence=rec.evidence,
        decide_after_s=2.0,
        default_action="apply",
        verb_kwargs=rec.verb_kwargs,
    )
    assert res.resolution == "overridden"
    assert res.override_verb == "truncate"
    plan_now = st.get_tonight_plan()
    targets_now = [t["target_name"] for t in (plan_now.get("visible_targets") or [])]
    print(f"[7] operator override applied: {res.adaptation.summary}; targets now {targets_now}")
    assert targets_now == ["Vega"]   # truncate kept Vega, dropped M27

    print()
    print("Doctrine wiring end-to-end OK")


if __name__ == "__main__":
    raise SystemExit(main())
