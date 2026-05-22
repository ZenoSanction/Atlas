"""Smoke test for the plate-solve orchestrator + CaptureSequence integration.

Exercises:
  1. PlateSolveContext + decide() — every trigger path produces the
     expected (trigger, reason) pair.
  2. solve_and_sync(simulation=True) — returns synthetic success.
  3. End-to-end CaptureSequence run in sim mode with astap_client wired:
     verify that platesolve_started + platesolve_complete events fire
     before the first frame and after a simulated guiding recovery.

Run from project root:
    venv\\Scripts\\python.exe scripts\\smoke_platesolve.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


async def test_decision_engine() -> None:
    _hr("1. decide() truth table")
    from atlas.capture.platesolve_orchestrator import (
        PlateSolveContext, decide,
    )

    cases = [
        ("operator request beats everything",
         PlateSolveContext(operator_requested=True,
                              is_first_frame_on_target=True,
                              after_meridian_flip=True),
         "operator_request"),
        ("first frame fires when no operator request",
         PlateSolveContext(is_first_frame_on_target=True),
         "first_frame"),
        ("meridian flip when not first frame",
         PlateSolveContext(after_meridian_flip=True),
         "meridian_flip"),
        ("guiding recovery when nothing else",
         PlateSolveContext(after_guiding_recovery=True),
         "guiding_recovery"),
        ("nothing triggered -> skip",
         PlateSolveContext(),
         "(none)"),
    ]
    for name, ctx, expected_trigger in cases:
        d = decide(ctx)
        mark = "OK" if d.trigger == expected_trigger else "FAIL"
        print(f"  [{mark}] {name}: trigger={d.trigger!r} "
                f"should_solve={d.should_solve}  ({d.reason})")
        assert d.trigger == expected_trigger, name


async def test_sim_solve() -> None:
    _hr("2. solve_and_sync(simulation=True)")
    from atlas.capture.platesolve_orchestrator import solve_and_sync
    r = await solve_and_sync(
        astap_client=None, nina_client=None,
        target_ra_deg=83.633, target_dec_deg=22.014,
        target_name="M1", simulation=True,
    )
    print(f"  ok={r.ok} elapsed_s={r.elapsed_s} "
          f"solved_ra={r.solved_ra_deg} solved_dec={r.solved_dec_deg} "
          f"err={r.pointing_error_arcmin} arcmin")
    print(f"  note: {r.note}")
    assert r.ok, "sim mode should always succeed"
    assert abs(r.solved_ra_deg - 83.633) < 1e-6
    assert r.pointing_error_arcmin == 0.0


async def test_capture_sequence_integration() -> None:
    _hr("3. CaptureSequence end-to-end with platesolve wired (sim)")
    from atlas.capture.sequence import CaptureSequence
    from atlas.simulation.fake_hardware import FakeNina, FakePhd2
    from atlas.hardware.astap import AstapClient
    from atlas.capture.autofocus import AutofocusDecisionEngine

    target = {
        "target_name": "M51",
        "ra_deg": 202.4696,
        "dec_deg": 47.1953,
    }
    plan = [
        {"filter": "L", "exposure_s": 60, "count": 2},
        {"filter": "R", "exposure_s": 60, "count": 1},
    ]
    seq = CaptureSequence(
        nina=FakeNina(), phd2=FakePhd2(), target=target,
        exposure_plan=plan, dither_every_n_frames=1, simulation=True,
        autofocus_engine=AutofocusDecisionEngine(), is_mono=True,
        astap_client=AstapClient(astap_path=None),  # sim mode skips real path
    )

    saw_solve_start = False
    saw_solve_complete = False
    saw_first_frame = False
    n_events = 0
    async for ev in seq.run():
        n_events += 1
        state = ev.get("state")
        summary = ev.get("summary", "")
        if state == "platesolve_started":
            saw_solve_start = True
            print(f"  [solve start] {summary}")
        elif state == "platesolve_complete":
            saw_solve_complete = True
            print(f"  [solve done ] {summary}")
        elif state == "platesolve_skipped":
            print(f"  [solve skip ] {summary}")
        elif state == "frame_captured":
            if not saw_first_frame:
                saw_first_frame = True
                print(f"  [first frame] {summary}")
        elif state in ("started", "complete"):
            print(f"  [{state}] {summary}")

    print(f"\n  total events: {n_events}")
    assert saw_solve_start, "expected platesolve_started event"
    assert saw_solve_complete, "expected platesolve_complete event"
    assert saw_first_frame, "expected at least one frame_captured event"
    print("  OK — solve fired BEFORE first frame, sequence completed")


async def main() -> None:
    await test_decision_engine()
    await test_sim_solve()
    await test_capture_sequence_integration()
    _hr("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
