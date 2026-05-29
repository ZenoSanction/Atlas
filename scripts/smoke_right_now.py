"""Smoke test for the Right Now substrate.

Verifies:
  - get_state().get_right_now() returns a well-shaped dict
  - All three layers are present (situational / procedural / strategic)
  - Execution snapshot updates flow through
  - Pending decisions surface in the view
  - The shared get_right_now tool returns the same shape
  - The summary line stays under reasonable length

ASCII output only (no Unicode arrows etc.) so it runs on cp1252.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from atlas.agents.state import (
        get_state, ExecutionSnapshot, PendingDecision,
        OperatorVerdict, WeatherAssessment,
    )

    st = get_state()

    # --- 1. Empty initial state --------------------------------------------
    rn = st.get_right_now()
    assert "computed_at" in rn, "computed_at missing"
    for layer in ("situational", "procedural", "strategic"):
        assert layer in rn, f"layer missing: {layer}"
    assert rn["situational"]["verdict"] in ("UNKNOWN", "GO", "CAUTION", "NO-GO")
    assert rn["procedural"]["active_slot"] is None
    assert rn["strategic"]["plan_present"] is False
    assert rn["pending_decisions"] == []
    assert rn["blocked_reason"] is None
    print(f"[1] empty snapshot OK; summary: {rn['summary']!r}")

    # --- 2. Verdict + weather populate situational layer -------------------
    st.set_verdict(OperatorVerdict(
        decided_at="2026-05-29T01:00:00Z",
        verdict="GO",
        reason="clear skies, dew margin healthy",
        sources=["critic", "preflight"],
    ))
    st.set_assessment(WeatherAssessment(
        observed_at="2026-05-29T01:00:00Z",
        assessed_at="2026-05-29T01:00:00Z",
        overall_severity="ok",
        summary="Clear, 12C, wind 8 mph",
    ))
    rn = st.get_right_now()
    assert rn["situational"]["verdict"] == "GO"
    assert rn["situational"]["weather_severity"] == "ok"
    assert "Clear" in rn["situational"]["weather_summary"]
    print(f"[2] verdict + weather OK; summary: {rn['summary']!r}")

    # --- 3. Plan publishes -> strategic layer fills in ---------------------
    st.set_tonight_plan({
        "built_at": "2026-05-29T00:30:00Z",
        "reason": "startup",
        "visible_targets": [
            {"target_name": "M51", "workflow": "deepsky",
             "start_utc": "2026-05-29T02:00:00Z",
             "end_utc": "2026-05-29T03:30:00Z",
             "priority": 1, "ra_deg": 202.5, "dec_deg": 47.2,
             "scheduled_for_min": 90},
            {"target_name": "M13", "workflow": "deepsky",
             "start_utc": "2026-05-29T03:30:00Z",
             "end_utc": "2026-05-29T05:00:00Z",
             "priority": 2, "ra_deg": 250.4, "dec_deg": 36.5,
             "scheduled_for_min": 90},
        ],
        "considered_count": 12,
        "scheduled_total_min": 180,
        "dark_window_min": 300,
        "active_campaigns": 2,
    })
    st.set_session_review({
        "review_id": "rev-1",
        "state": "final",
        "advisories": [
            {"kind": "weather", "severity": "info",
             "source": "critic", "message": "wind trending up",
             "at": "2026-05-29T01:00:00Z"},
            {"kind": "moon", "severity": "warning",
             "source": "critic", "message": "moon 12 deg from M13",
             "at": "2026-05-29T01:00:00Z"},
        ],
    })
    rn = st.get_right_now()
    s = rn["strategic"]
    assert s["plan_present"] is True
    assert s["visible_target_count"] == 2
    assert s["scheduled_total_min"] == 180
    assert s["dark_window_min"] == 300
    assert s["fit_pct"] == 60.0
    assert s["advisory_count"] == 2
    assert s["advisory_counts"] == {"info": 1, "warning": 1, "critical": 0}
    print(f"[3] plan + advisories OK; fit_pct={s['fit_pct']}, "
          f"adv={s['advisory_counts']}")

    # --- 4. Execution snapshot updates flow through ------------------------
    st.update_execution(
        active_slot={
            "target_name": "M51", "workflow": "deepsky",
            "start_utc": "2026-05-29T02:00:00Z",
            "end_utc": "2026-05-29T03:30:00Z",
        },
        active_action="capturing L frame 18/30",
        active_frame={"filter": "L", "exposure_s": 120,
                      "index": 18, "count": 30},
        slot_progress={"elapsed_min": 36, "scheduled_min": 90,
                       "frames_done": 17, "frames_total": 30},
        next_action="slew to M13",
        next_action_at="2026-05-29T03:30:00Z",
        planned_session_end="2026-05-29T05:00:00Z",
    )
    rn = st.get_right_now()
    p = rn["procedural"]
    assert p["active_slot"]["target_name"] == "M51"
    assert p["active_action"] == "capturing L frame 18/30"
    assert p["active_frame"]["index"] == 18
    assert p["slot_progress"]["frames_done"] == 17
    assert p["next_action"] == "slew to M13"
    assert rn["situational"]["session_active"] is True
    print(f"[4] execution snapshot OK; summary: {rn['summary']!r}")

    # --- 5. Pending decision narration -------------------------------------
    pd = PendingDecision(
        id="pd-001",
        kind="pause",
        narration=("Considering: pausing the session. Wind has climbed "
                   "to 18 mph (threshold 20). Watching for 5 min "
                   "before deciding. Operator override available."),
        started_at="2026-05-29T03:00:00Z",
        decide_by="2026-05-29T03:05:00Z",
        default_action="continue",
        confidence_layer="rules",
        severity="info",
        evidence={"wind_mph": 18, "threshold_mph": 20,
                  "forecast": "climbing"},
    )
    st.post_pending_decision(pd)
    rn = st.get_right_now()
    assert len(rn["pending_decisions"]) == 1
    assert rn["pending_decisions"][0]["kind"] == "pause"
    assert "Wind" in rn["pending_decisions"][0]["narration"]
    assert "pending=1" in rn["summary"]
    print(f"[5] pending decision OK; summary: {rn['summary']!r}")

    # --- 6. Resolve the pending decision -----------------------------------
    ok = st.resolve_pending_decision("pd-001")
    assert ok
    rn = st.get_right_now()
    assert rn["pending_decisions"] == []
    print(f"[6] resolve pending OK; summary: {rn['summary']!r}")

    # --- 7. Blocked reason propagates --------------------------------------
    st.update_execution(blocked_reason="hardware: focuser lost connection")
    rn = st.get_right_now()
    assert rn["blocked_reason"] == "hardware: focuser lost connection"
    assert "blocked=" in rn["summary"]
    print(f"[7] blocked_reason OK; summary: {rn['summary']!r}")

    # --- 8. Shared tool returns the same shape -----------------------------
    from atlas.agents.shared_tools import GET_RIGHT_NOW_TOOL
    tool_out = asyncio.run(GET_RIGHT_NOW_TOOL.handler({}))
    assert tool_out["situational"]["verdict"] == "GO"
    assert tool_out["procedural"]["active_slot"]["target_name"] == "M51"
    assert tool_out["strategic"]["plan_present"] is True
    print(f"[8] shared tool OK; summary: {tool_out['summary']!r}")

    # --- 9. Clear execution wipes the slot ---------------------------------
    st.clear_execution()
    rn = st.get_right_now()
    assert rn["procedural"]["active_slot"] is None
    assert rn["blocked_reason"] is None
    print(f"[9] clear_execution OK; summary: {rn['summary']!r}")

    print("\nAll Right Now substrate checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"ASSERTION FAILED: {e}")
        raise SystemExit(1)
