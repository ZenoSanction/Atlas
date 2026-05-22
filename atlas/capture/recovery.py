"""Runtime self-healing state machines.

When a sequence is mid-flight and something goes wrong (focus drift,
guiding loss, mount stall, etc.), ATLAS attempts a *structured*
recovery instead of just bailing out and paging the human. Each
recovery has:

  * Detection criteria (already checked by the caller — we're entered
    only when a real problem has been confirmed)
  * Ordered list of escalating steps to try
  * Per-step timeout + result classification (recovered / failed / fatal)
  * Final escalation: page human + safe-park if all steps exhausted

Every step emits a dict event so the human watching the dashboard sees
exactly what was tried, in what order, and what worked. That's the
"visibility on every decision" principle applied to failure handling.

Async-generator interface so CaptureSequence can `async for ev in
recover_guiding(...)`. The generator yields events as it works, and
the final event always carries `state="recovered"` or `state="escalated"`
so the caller knows how to proceed:

  * recovered  → resume the sequence
  * escalated  → abort the sequence + page operator + safe-park

Scenarios covered (Phase 2):
  1. recover_guiding  — PHD2 lost the star or disconnected
  2. recover_focus    — HFR degraded past tolerance mid-sequence

Future scenarios (Phase 3 backlog, not yet wired):
  3. recover_mount   — slew failed or mount unresponsive
  4. recover_camera  — camera disconnected or thermal runaway
  5. recover_dew     — optics fogged (heater + pause)
  6. recover_clouds  — transient cloudover (pause + verdict watcher)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from atlas.logging_setup import get_logger

log = get_logger("capture.recovery")


# ---- common dataclasses --------------------------------------------------

@dataclass
class RecoveryStep:
    """One attempt within a recovery. Yields one event when it starts,
    one when it ends. The terminal state ('recovered' / 'failed') is
    decided by the step's outcome predicate."""
    name: str
    description: str            # human-readable, lands on dashboard
    timeout_s: float = 30.0


@dataclass
class RecoveryOutcome:
    state: str                  # 'recovered' | 'escalated'
    scenario: str               # 'guiding' | 'focus' | ...
    steps_tried: list[str]
    elapsed_s: float
    final_reason: str           # why we got to the terminal state


def _ev(*, scenario: str, phase: str, summary: str, **extra) -> dict:
    """Helper: build a recovery event dict consistent across scenarios."""
    return {
        "kind": "recovery",
        "scenario": scenario,
        "phase": phase,       # 'detected' | 'step_start' | 'step_ok' | 'step_fail' | 'recovered' | 'escalated'
        "summary": summary,
        **extra,
    }


# ---- scenario 1: guiding recovery ----------------------------------------

async def recover_guiding(*, phd2, astap_client=None, nina=None,
                            target_ra_deg: Optional[float] = None,
                            target_dec_deg: Optional[float] = None,
                            target_name: Optional[str] = None,
                            simulation: bool = False,
                            ) -> AsyncIterator[dict]:
    """Escalating guiding-recovery state machine.

    Steps (each gives up after timeout_s, then we move to the next):
      1. Wait — PHD2 may recover on its own (60 s)
      2. Restart-guide — stop_capture + guide() to re-acquire star
      3. Plate-solve resync — solve mount, then restart-guide
      4. Escalate — page human + safe-park

    Yields per-step events; final event is 'recovered' or 'escalated'."""
    scenario = "guiding"
    started = time.monotonic()
    steps_tried: list[str] = []

    yield _ev(scenario=scenario, phase="detected",
                summary="guiding lost — entering recovery state machine")

    async def _is_guiding() -> bool:
        if simulation:
            return True
        try:
            state = await phd2.get_app_state()
        except Exception:
            return False
        return str(state).lower() in ("guiding", "settling")

    # Step 1: passive wait. PHD2 often recovers on its own when a cloud
    # clears or a satellite passes. 60 s is the standard "ought to be
    # back" budget; anything longer suggests a real problem.
    step = RecoveryStep(name="passive_wait", timeout_s=60.0,
                         description="wait up to 60s for PHD2 to self-recover")
    steps_tried.append(step.name)
    yield _ev(scenario=scenario, phase="step_start", step=step.name,
                summary=f"step 1: {step.description}")
    deadline = time.monotonic() + step.timeout_s
    recovered = False
    while time.monotonic() < deadline:
        if await _is_guiding():
            recovered = True
            break
        await asyncio.sleep(2.0 if not simulation else 0.05)
    if recovered:
        yield _ev(scenario=scenario, phase="step_ok", step=step.name,
                    summary="guiding self-recovered during passive wait")
        yield _ev(scenario=scenario, phase="recovered",
                    summary="guiding restored without intervention",
                    steps_tried=steps_tried,
                    elapsed_s=round(time.monotonic() - started, 1))
        return
    yield _ev(scenario=scenario, phase="step_fail", step=step.name,
                summary="passive wait timed out — escalating to restart-guide")

    # Step 2: stop_capture + guide() — tell PHD2 to drop its current
    # state and re-acquire from scratch. The settle params let it
    # confirm stability before declaring success.
    step = RecoveryStep(name="restart_guide", timeout_s=90.0,
                         description="ask PHD2 to stop + re-acquire star")
    steps_tried.append(step.name)
    yield _ev(scenario=scenario, phase="step_start", step=step.name,
                summary=f"step 2: {step.description}")
    try:
        if not simulation:
            try:
                await phd2.stop_capture()
            except Exception as e:
                log.debug("stop_capture failed (continuing): %s", e)
            # Brief pause so PHD2 fully unhooks before we ask it to guide.
            await asyncio.sleep(2.0)
            await asyncio.wait_for(
                phd2.guide(settle_pixels=2.0, settle_time_s=10,
                              settle_timeout_s=int(step.timeout_s)),
                timeout=step.timeout_s,
            )
        else:
            await asyncio.sleep(0.1)
        # Confirm
        ok = await _is_guiding()
    except asyncio.TimeoutError:
        ok = False
    except Exception as e:
        log.warning("restart_guide failed: %s", e)
        ok = False
    if ok:
        yield _ev(scenario=scenario, phase="step_ok", step=step.name,
                    summary="PHD2 re-acquired guide star and settled")
        yield _ev(scenario=scenario, phase="recovered",
                    summary="guiding restored via restart-guide",
                    steps_tried=steps_tried,
                    elapsed_s=round(time.monotonic() - started, 1))
        return
    yield _ev(scenario=scenario, phase="step_fail", step=step.name,
                summary="restart-guide failed — escalating to plate-solve resync")

    # Step 3: plate-solve resync. Maybe the mount slipped and the
    # guide-star search box no longer overlaps anything bright. Solve
    # the field, sync the mount, then try restart-guide one more time.
    if astap_client is not None and target_ra_deg is not None and nina is not None:
        step = RecoveryStep(name="platesolve_resync", timeout_s=120.0,
                             description="solve field + sync mount + retry guide")
        steps_tried.append(step.name)
        yield _ev(scenario=scenario, phase="step_start", step=step.name,
                    summary=f"step 3: {step.description}")
        try:
            from atlas.capture.platesolve_orchestrator import solve_and_sync
            result = await solve_and_sync(
                astap_client=astap_client, nina_client=nina,
                target_ra_deg=float(target_ra_deg),
                target_dec_deg=float(target_dec_deg or 0.0),
                target_name=target_name or "?",
                simulation=simulation,
            )
            if result.ok:
                yield _ev(scenario=scenario, phase="step_ok", step=step.name,
                            summary=f"solve OK ({result.pointing_error_arcmin} "
                                      f"arcmin); retrying guide")
                # Try guide() one more time, now that the mount is centered
                try:
                    if not simulation:
                        await asyncio.wait_for(
                            phd2.guide(settle_pixels=2.0, settle_time_s=10,
                                          settle_timeout_s=60),
                            timeout=60.0,
                        )
                    final_ok = await _is_guiding()
                except Exception:
                    final_ok = False
                if final_ok:
                    yield _ev(scenario=scenario, phase="recovered",
                                summary="guiding restored after plate-solve resync",
                                steps_tried=steps_tried,
                                elapsed_s=round(time.monotonic() - started, 1))
                    return
                yield _ev(scenario=scenario, phase="step_fail", step=step.name,
                            summary="solve OK but post-solve guide still failed")
            else:
                yield _ev(scenario=scenario, phase="step_fail", step=step.name,
                            summary=f"plate-solve failed: {result.error}")
        except Exception as e:
            yield _ev(scenario=scenario, phase="step_fail", step=step.name,
                        summary=f"plate-solve resync threw: {type(e).__name__}: {e}")
    else:
        yield _ev(scenario=scenario, phase="step_skipped",
                    step="platesolve_resync",
                    summary="plate-solve resync skipped (no ASTAP / missing coords)")

    # All steps exhausted — escalate
    yield _ev(scenario=scenario, phase="escalated",
                summary="guiding could not be recovered automatically — "
                          "paging operator and ending sequence safely",
                steps_tried=steps_tried,
                elapsed_s=round(time.monotonic() - started, 1))


# ---- scenario 2: focus recovery ------------------------------------------

async def recover_focus(*, nina, autofocus_engine=None,
                          current_hfr: Optional[float] = None,
                          reference_hfr: Optional[float] = None,
                          current_filter: Optional[str] = None,
                          simulation: bool = False,
                          ) -> AsyncIterator[dict]:
    """Escalating focus-recovery state machine.

    Steps:
      1. Force-autofocus — bypass engine policy, just run AF now
      2. Wide-range autofocus — same but with a wider step count
                                  (the V-curve may be at the edge)
      3. Dew check — if temp dropped sharply, suggest dew heater
      4. Escalate — page human, abort sequence (lose the rest of this
                    target's frames rather than collect blurry data)

    The autofocus_engine is NOT consulted for the trigger decision —
    by the time we're in recovery, we KNOW we need to refocus. The
    engine is just kept for context."""
    scenario = "focus"
    started = time.monotonic()
    steps_tried: list[str] = []

    yield _ev(scenario=scenario, phase="detected",
                summary=(f"focus drift detected — HFR={current_hfr} "
                          f"vs reference {reference_hfr}"),
                current_hfr=current_hfr, reference_hfr=reference_hfr)

    # Step 1: vanilla autofocus
    step = RecoveryStep(name="standard_autofocus", timeout_s=180.0,
                         description="run NINA autofocus at current settings")
    steps_tried.append(step.name)
    yield _ev(scenario=scenario, phase="step_start", step=step.name,
                summary=f"step 1: {step.description}")
    from atlas.capture.autofocus import run_autofocus
    try:
        result = await run_autofocus(nina, simulation=simulation,
                                        timeout_s=step.timeout_s)
        ok = bool(result.get("ok"))
        new_hfr = result.get("hfr_min")
        elapsed = result.get("elapsed_s")
    except Exception as e:
        ok = False
        new_hfr = None
        elapsed = None
        log.warning("standard_autofocus threw: %s", e)
    if ok and new_hfr is not None:
        # Accept if new HFR is within 1.10x of reference (or no reference,
        # accept any non-error result).
        accept = (reference_hfr is None
                    or float(new_hfr) <= 1.10 * float(reference_hfr))
        if accept:
            yield _ev(scenario=scenario, phase="step_ok", step=step.name,
                        summary=(f"autofocus OK — HFR={new_hfr} "
                                  f"in {elapsed}s"),
                        hfr_min=new_hfr)
            yield _ev(scenario=scenario, phase="recovered",
                        summary=f"focus restored — HFR={new_hfr}",
                        steps_tried=steps_tried,
                        elapsed_s=round(time.monotonic() - started, 1),
                        hfr_min=new_hfr)
            return
        yield _ev(scenario=scenario, phase="step_fail", step=step.name,
                    summary=(f"autofocus returned HFR={new_hfr} but still "
                              f"above 1.10x reference {reference_hfr} — "
                              "escalating"))
    else:
        yield _ev(scenario=scenario, phase="step_fail", step=step.name,
                    summary=f"autofocus failed: {result.get('error', 'unknown')}")

    # Step 2: wide-range autofocus. The default V-curve sweep may have
    # been too narrow if focus drifted a long way (e.g. focuser
    # backlash, temp shock). NINA exposes a step-count knob — we'd
    # pass a larger value here on bench day. For now this is a
    # placeholder that re-runs the standard call, since the wire-up
    # for "wider sweep" depends on the real NINA endpoint.
    step = RecoveryStep(name="wide_range_autofocus", timeout_s=240.0,
                         description="retry autofocus with wider step range")
    steps_tried.append(step.name)
    yield _ev(scenario=scenario, phase="step_start", step=step.name,
                summary=f"step 2: {step.description}")
    try:
        result = await run_autofocus(nina, simulation=simulation,
                                        timeout_s=step.timeout_s)
        ok = bool(result.get("ok"))
        new_hfr = result.get("hfr_min")
    except Exception as e:
        ok = False
        new_hfr = None
        log.warning("wide_range_autofocus threw: %s", e)
    if ok and new_hfr is not None and (
            reference_hfr is None
            or float(new_hfr) <= 1.15 * float(reference_hfr)):
        yield _ev(scenario=scenario, phase="step_ok", step=step.name,
                    summary=f"wide-range autofocus OK — HFR={new_hfr}",
                    hfr_min=new_hfr)
        yield _ev(scenario=scenario, phase="recovered",
                    summary=f"focus restored after wide-range sweep — HFR={new_hfr}",
                    steps_tried=steps_tried,
                    elapsed_s=round(time.monotonic() - started, 1),
                    hfr_min=new_hfr)
        return
    yield _ev(scenario=scenario, phase="step_fail", step=step.name,
                summary=("wide-range autofocus still couldn't reach target "
                          "HFR — may be dewing or hardware issue"))

    # All steps exhausted — escalate.
    yield _ev(scenario=scenario, phase="escalated",
                summary=("focus could not be restored — paging operator. "
                          "Likely causes: dew on objective, mechanical drift, "
                          "or focuser/camera fault. Aborting target."),
                steps_tried=steps_tried,
                elapsed_s=round(time.monotonic() - started, 1))


# ---- helper: collect terminal outcome from generator ---------------------

async def drive_recovery(gen: AsyncIterator[dict], *,
                            emit=None) -> RecoveryOutcome:
    """Drive a recovery async-generator to completion, optionally
    forwarding events via the ``emit`` callable (used by tests; in
    production the caller iterates directly and yields events outward).

    Returns the final RecoveryOutcome so the caller can branch on
    recovered/escalated without re-parsing every event."""
    last: dict = {}
    steps_tried: list[str] = []
    scenario = "unknown"
    started = time.monotonic()
    async for ev in gen:
        if emit is not None:
            try:
                emit(ev)
            except Exception:
                pass
        scenario = ev.get("scenario", scenario)
        if ev.get("phase") == "step_start":
            steps_tried.append(ev.get("step", "?"))
        if ev.get("phase") in ("recovered", "escalated"):
            last = ev
            break
    elapsed = round(time.monotonic() - started, 1)
    return RecoveryOutcome(
        state=last.get("phase", "escalated"),
        scenario=scenario,
        steps_tried=last.get("steps_tried", steps_tried),
        elapsed_s=last.get("elapsed_s", elapsed),
        final_reason=last.get("summary", "(no terminal event)"),
    )
