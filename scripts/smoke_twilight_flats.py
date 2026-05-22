"""Smoke test for the twilight-flat orchestrator.

Drives the orchestrator in sim mode with a clock fixture that
forces 'sun altitude = -5 deg' for the duration of the test so the
window stays open.

Tests:
  1. plan_window returns sane evening/morning windows.
  2. _adjust_exposure tunes toward target ADU.
  3. _frame_accepted ADU tolerance band logic.
  4. Full orchestrator run in sim mode: yields started -> filter
     events -> complete; produces accepted frames in steady ADU.
  5. abort() short-circuits the next loop iteration.

Run from project root:
    venv\\Scripts\\python.exe scripts\\smoke_twilight_flats.py
"""
from __future__ import annotations

import asyncio
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


# Site fixture — NYC for the planner test.
LAT = 40.75
LON = -73.98


async def test_plan_window() -> None:
    _hr("1. plan_window returns sane crossings")
    from atlas.calibration.twilight_flats import plan_window
    # Evening of a known date
    when = datetime(2026, 5, 22, 22, 0, 0)
    p = plan_window(direction="evening", when_utc=when,
                       latitude_deg=LAT, longitude_deg=LON,
                       filter_order=["L", "R", "G", "B"])
    print(f"  evening plan: {p.summary}")
    assert p.window_start_utc is not None
    assert p.window_stop_utc is not None
    assert p.window_stop_utc > p.window_start_utc
    # Window duration is a few minutes (sun moves ~15 deg/hour at low lats)
    dur_min = (p.window_stop_utc - p.window_start_utc).total_seconds() / 60.0
    assert 5 < dur_min < 60, f"unexpected window length: {dur_min} min"

    # Morning: filter order reversed
    p2 = plan_window(direction="morning", when_utc=when,
                       latitude_deg=LAT, longitude_deg=LON,
                       filter_order=["L", "R", "G", "B"])
    print(f"  morning plan: {p2.summary}")
    print(f"  morning order (narrowband first): {p2.filter_order}")
    assert p2.filter_order == ["B", "G", "R", "L"]


async def test_exposure_tuning() -> None:
    _hr("2. _adjust_exposure tunes toward target ADU")
    from atlas.calibration.twilight_flats import TwilightFlatOrchestrator
    from atlas.simulation.fake_hardware import FakeNina
    orch = TwilightFlatOrchestrator(
        nina=FakeNina(), latitude_deg=LAT, longitude_deg=LON,
        simulation=True,
    )
    # Probe at 1s read 15000 ADU -> should double the exposure
    new = orch._adjust_exposure(current_exposure=1.0, measured_adu=15000.0)
    print(f"  1s @ 15k ADU -> {new:.2f}s (expect ~2.0s)")
    assert 1.9 < new < 2.1
    # Probe at 1s read 60000 ADU -> should halve
    new2 = orch._adjust_exposure(current_exposure=1.0, measured_adu=60000.0)
    print(f"  1s @ 60k ADU -> {new2:.2f}s (expect ~0.5s)")
    assert 0.4 < new2 < 0.6
    # Clamping at min
    new3 = orch._adjust_exposure(current_exposure=0.5, measured_adu=120000.0)
    print(f"  0.5s @ 120k ADU -> {new3:.2f}s (expect MIN_EXPOSURE_S=0.5)")
    assert new3 == 0.5


async def test_acceptance() -> None:
    _hr("3. _frame_accepted ADU tolerance band")
    from atlas.calibration.twilight_flats import (
        TwilightFlatOrchestrator, TARGET_ADU, ADU_TOLERANCE_PCT,
    )
    from atlas.simulation.fake_hardware import FakeNina
    orch = TwilightFlatOrchestrator(
        nina=FakeNina(), latitude_deg=LAT, longitude_deg=LON,
        simulation=True,
    )
    lo = TARGET_ADU * (1.0 - ADU_TOLERANCE_PCT / 100.0)
    hi = TARGET_ADU * (1.0 + ADU_TOLERANCE_PCT / 100.0)
    print(f"  acceptance band: {lo:.0f} - {hi:.0f}")
    assert orch._frame_accepted(TARGET_ADU) is True
    assert orch._frame_accepted(lo + 1) is True
    assert orch._frame_accepted(hi - 1) is True
    assert orch._frame_accepted(lo - 1) is False
    assert orch._frame_accepted(hi + 1) is False
    assert orch._frame_accepted(None) is False


async def test_orchestrator_run() -> None:
    _hr("4. Full orchestrator run (sim) — sun forced to -5 deg")
    from atlas.calibration import twilight_flats as tf
    from atlas.simulation.fake_hardware import FakeNina

    # Monkey-patch sun_altitude to keep window open
    import atlas.astronomy.visibility as vis
    original = vis.sun_altitude
    vis.sun_altitude = lambda *a, **k: -5.0

    try:
        orch = tf.TwilightFlatOrchestrator(
            nina=FakeNina(), latitude_deg=LAT, longitude_deg=LON,
            direction="evening",
            filter_order=["L", "R"],
            n_frames_per_filter=3,    # keep test fast
            simulation=True,
        )
        events_by_phase: dict[str, int] = {}
        last_summary = ""
        async for ev in orch.run():
            phase = ev.get("phase")
            events_by_phase[phase] = events_by_phase.get(phase, 0) + 1
            last_summary = ev.get("summary", "")
        print(f"  event phases: {events_by_phase}")
        print(f"  final: {last_summary}")
        assert "started" in events_by_phase
        assert "complete" in events_by_phase
        assert events_by_phase.get("filter_started", 0) == 2
        assert events_by_phase.get("filter_complete", 0) == 2
        # Expect some flat_accepted events
        accepted = events_by_phase.get("flat_accepted", 0)
        rejected = events_by_phase.get("flat_rejected", 0)
        print(f"  accepted={accepted} rejected={rejected}")
        # Sim ADU should mostly land in band after tuning
        assert accepted >= 4, "expected at least 4 accepted flats across 2 filters"
    finally:
        vis.sun_altitude = original


async def test_abort() -> None:
    _hr("5. abort() short-circuits remaining filters")
    from atlas.calibration import twilight_flats as tf
    from atlas.simulation.fake_hardware import FakeNina

    import atlas.astronomy.visibility as vis
    original = vis.sun_altitude
    vis.sun_altitude = lambda *a, **k: -5.0

    try:
        orch = tf.TwilightFlatOrchestrator(
            nina=FakeNina(), latitude_deg=LAT, longitude_deg=LON,
            direction="evening",
            filter_order=["L", "R", "G", "B"],
            n_frames_per_filter=3,
            simulation=True,
        )
        # Abort after first filter event
        saw_filter_started = 0
        saw_aborted = False
        async for ev in orch.run():
            if ev.get("phase") == "filter_started":
                saw_filter_started += 1
                if saw_filter_started == 1:
                    orch.abort()
            if ev.get("phase") == "aborted":
                saw_aborted = True
                break
        print(f"  filter_started events: {saw_filter_started}")
        print(f"  saw aborted: {saw_aborted}")
        # We may complete the first filter before checking abort,
        # but we should not get to all four
        assert saw_filter_started < 4
    finally:
        vis.sun_altitude = original


async def main() -> None:
    await test_plan_window()
    await test_exposure_tuning()
    await test_acceptance()
    await test_orchestrator_run()
    await test_abort()
    _hr("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
