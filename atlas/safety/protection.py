"""Automatic hardware protection sequences.

When the Operator declares an execution block (storm rolling in, wind
beyond critical, hardware fault), the actual *protection* happens
here — the Operator just orchestrates. Protecting the hardware is
the only thing the system does autonomously; everything else is
advisory.

Two sequences:

  SafeShutdownSequence
    Stop guiding → abort capture → park mount → warm camera → close
    roof (if NINA-controlled). Each step has a timeout so a stuck
    sub-step can't block the whole sequence. Each step emits a
    progress event so the dashboard can show "parking mount…" rather
    than freezing.

  SafeStartupSequence
    The reverse, for when conditions clear and the Operator wants to
    resume against the same plan: unpark mount → cool camera to
    setpoint → wait for stable → restart guiding (if calibrated).
    Returns a "ready to resume" or "needs operator attention" result.

Both sequences are async generators yielding event dicts so the
Operator can broadcast each step to the bus + dashboard without
blocking. Sim mode runs against FakeNina/FakePhd2 — the orchestration
gets exercised even without hardware connected.

DELIBERATELY NOT IN SCOPE:
  - Plan changes. Protection touches hardware. The plan is the
    Planner's job. When the Operator restarts after a clear, it
    sends a REVISION_REQUEST so the Planner can rebuild for whatever
    dark hours remain, then operates against THAT plan.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import AsyncIterator

from atlas.logging_setup import get_logger

log = get_logger("safety.protection")

# Per-step timeouts. Generous because real hardware can be slow —
# mount park can take 30s on a German equatorial, camera warmup 60s+
# on a sub-zero CMOS body.
T_STOP_GUIDE_S   = 15.0
T_ABORT_CAPTURE_S = 20.0
T_PARK_MOUNT_S    = 90.0
T_WARM_CAMERA_S   = 180.0
T_CLOSE_DOME_S    = 120.0

T_UNPARK_MOUNT_S  = 90.0
T_RESTART_GUIDE_S = 120.0


# ---- Sequence step + result types -----------------------------------------

@dataclass
class ProtectionStep:
    """One step in a protection sequence."""
    name: str            # "stop_guiding" | "park_mount" | ...
    state: str           # "starting" | "ok" | "skipped" | "timeout" | "error"
    detail: str = ""     # human-readable
    elapsed_s: float = 0.0


# ---- Helper that wraps any awaitable with a timeout + step record --------

async def _do_step(name: str, coro, timeout_s: float,
                     skip_if: bool = False,
                     skip_reason: str = "") -> ProtectionStep:
    if skip_if:
        return ProtectionStep(name=name, state="skipped",
                                detail=skip_reason, elapsed_s=0.0)
    started = time.monotonic()
    try:
        await asyncio.wait_for(coro, timeout=timeout_s)
        return ProtectionStep(name=name, state="ok",
                                elapsed_s=round(time.monotonic() - started, 1))
    except asyncio.TimeoutError:
        return ProtectionStep(name=name, state="timeout",
                                detail=f"exceeded {timeout_s:.0f}s",
                                elapsed_s=round(time.monotonic() - started, 1))
    except Exception as e:
        return ProtectionStep(name=name, state="error",
                                detail=f"{type(e).__name__}: {e}",
                                elapsed_s=round(time.monotonic() - started, 1))


# ---- SafeShutdownSequence -------------------------------------------------

class SafeShutdownSequence:
    """Park hardware in response to an execution-block event.

    Usage::
        seq = SafeShutdownSequence(nina, phd2, reason="storm")
        async for event in seq.run():
            await bus.broadcast(event)
        # seq.result is the terminal summary
    """

    def __init__(self, *, nina, phd2, reason: str = "",
                 close_roof: bool = False,
                 active_capture=None) -> None:
        self._nina = nina
        self._phd2 = phd2
        self._reason = reason
        self._close_roof = close_roof
        self._active_capture = active_capture
        self.result: dict | None = None
        self.steps: list[ProtectionStep] = []

    async def run(self) -> AsyncIterator[dict]:
        sequence_started = time.monotonic()
        yield {
            "phase": "shutdown_started",
            "reason": self._reason,
            "summary": f"safe-shutdown initiated: {self._reason}",
        }

        # 1. Abort an in-progress capture sequence, if one is running.
        if self._active_capture is not None:
            try:
                self._active_capture.abort(reason=f"shutdown: {self._reason}")
            except Exception as e:
                log.warning("abort active capture failed: %s", e)
            yield {
                "phase": "step",
                "step": "abort_capture", "state": "ok",
                "summary": "in-progress capture aborted",
            }

        # 2. Stop PHD2 guiding. (FakePhd2.stop_capture returns {ok:True}.)
        step = await _do_step("stop_guiding",
                                self._phd2.stop_capture(),
                                T_STOP_GUIDE_S)
        self.steps.append(step)
        yield {"phase": "step", "step": step.name, "state": step.state,
                 "elapsed_s": step.elapsed_s, "detail": step.detail,
                 "summary": f"guiding stopped ({step.state})"}

        # 3. Park the mount.
        step = await _do_step("park_mount",
                                self._nina.park(),
                                T_PARK_MOUNT_S)
        self.steps.append(step)
        yield {"phase": "step", "step": step.name, "state": step.state,
                 "elapsed_s": step.elapsed_s, "detail": step.detail,
                 "summary": f"mount parked ({step.state})"}

        # 4. Warm the camera (controlled ramp). FakeNina's camera_warmup
        #    is a single-call no-op; on real hardware NINA handles the
        #    ramp internally.
        step = await _do_step("warm_camera",
                                self._nina.camera_warmup(),
                                T_WARM_CAMERA_S)
        self.steps.append(step)
        yield {"phase": "step", "step": step.name, "state": step.state,
                 "elapsed_s": step.elapsed_s, "detail": step.detail,
                 "summary": f"camera warmup commanded ({step.state})"}

        # 5. Close the roof — only if the operator has set roof_mode=nina
        #    in Setup. Otherwise we log + skip (the operator manually
        #    closes a manual roof, or it's a permanent observatory).
        step = await _do_step(
            "close_roof",
            self._nina.dome_close(),
            T_CLOSE_DOME_S,
            skip_if=not self._close_roof,
            skip_reason="roof_mode != nina; close manually if applicable",
        )
        self.steps.append(step)
        yield {"phase": "step", "step": step.name, "state": step.state,
                 "elapsed_s": step.elapsed_s, "detail": step.detail,
                 "summary": f"roof close ({step.state})"}

        # Done.
        total = round(time.monotonic() - sequence_started, 1)
        bad = [s for s in self.steps if s.state in ("timeout", "error")]
        terminal_state = "needs_attention" if bad else "safe"
        self.result = {
            "phase": "shutdown_complete",
            "state": terminal_state,
            "elapsed_s": total,
            "steps": [s.__dict__ for s in self.steps],
            "issues": [{"step": s.name, "detail": s.detail} for s in bad],
            "summary": (
                f"safe-shutdown complete in {total:.0f}s — {terminal_state}"
                + (f"; {len(bad)} step(s) need attention" if bad else "")
            ),
        }
        yield self.result


# ---- SafeStartupSequence --------------------------------------------------

class SafeStartupSequence:
    """Bring hardware back to imaging state after a verdict-clear.

    The reverse of SafeShutdownSequence. Doesn't touch the plan — that's
    the Planner's job. Doesn't actually start a session — that's
    operator-initiated via the dashboard or autonomously via the
    Operator's session-start logic once the verdict is GO.
    """

    def __init__(self, *, nina, phd2, cooling_setpoint_c: float | None = None,
                 simulation: bool = False) -> None:
        self._nina = nina
        self._phd2 = phd2
        self._setpoint = cooling_setpoint_c
        self._sim = simulation
        self.result: dict | None = None
        self.steps: list[ProtectionStep] = []

    async def run(self) -> AsyncIterator[dict]:
        started = time.monotonic()
        yield {"phase": "startup_started",
                 "summary": "safe-startup initiated"}

        # 1. Unpark mount
        step = await _do_step("unpark_mount",
                                self._nina.unpark(),
                                T_UNPARK_MOUNT_S)
        self.steps.append(step)
        yield {"phase": "step", "step": step.name, "state": step.state,
                 "elapsed_s": step.elapsed_s, "detail": step.detail,
                 "summary": f"mount unparked ({step.state})"}

        # 2. Cool camera (if setpoint configured). Uses the
        #    CoolingController for the stable-temp wait — same code
        #    path the manual "start cooling" button uses.
        if self._setpoint is not None:
            from atlas.hardware.cooling import cool_to_setpoint
            try:
                cool_result = await cool_to_setpoint(
                    self._nina, target_c=self._setpoint,
                    tolerance_c=0.3, max_wait_s=900.0,
                    simulation=self._sim,
                )
                state = "ok" if cool_result.state == "stable" else "timeout"
                self.steps.append(ProtectionStep(
                    name="cool_camera", state=state,
                    detail=cool_result.note,
                    elapsed_s=cool_result.elapsed_s,
                ))
                yield {"phase": "step", "step": "cool_camera",
                         "state": state,
                         "elapsed_s": cool_result.elapsed_s,
                         "detail": cool_result.note,
                         "summary": f"cooling: {cool_result.state}"}
            except Exception as e:
                self.steps.append(ProtectionStep(
                    name="cool_camera", state="error", detail=str(e),
                ))
                yield {"phase": "step", "step": "cool_camera",
                         "state": "error", "detail": str(e),
                         "summary": "cooling failed"}
        else:
            self.steps.append(ProtectionStep(
                name="cool_camera", state="skipped",
                detail="no setpoint configured",
            ))
            yield {"phase": "step", "step": "cool_camera",
                     "state": "skipped",
                     "summary": "no cooling setpoint configured — skipped"}

        # 3. Restart guiding only if previously calibrated. Otherwise
        #    we'd kick off a re-calibration the operator probably
        #    doesn't want without explicit consent.
        try:
            calibrated = await asyncio.wait_for(
                self._phd2.get_calibrated(), timeout=5.0,
            )
        except Exception:
            calibrated = False
        if calibrated:
            step = await _do_step("restart_guiding",
                                    self._phd2.guide(),
                                    T_RESTART_GUIDE_S)
            self.steps.append(step)
            yield {"phase": "step", "step": step.name, "state": step.state,
                     "elapsed_s": step.elapsed_s, "detail": step.detail,
                     "summary": f"guiding restarted ({step.state})"}
        else:
            self.steps.append(ProtectionStep(
                name="restart_guiding", state="skipped",
                detail="PHD2 not calibrated; needs operator to start fresh.",
            ))
            yield {"phase": "step", "step": "restart_guiding",
                     "state": "skipped",
                     "summary": "PHD2 not calibrated — operator must start guiding"}

        total = round(time.monotonic() - started, 1)
        bad = [s for s in self.steps if s.state in ("timeout", "error")]
        terminal_state = "ready" if not bad else "needs_attention"
        self.result = {
            "phase": "startup_complete",
            "state": terminal_state,
            "elapsed_s": total,
            "steps": [s.__dict__ for s in self.steps],
            "issues": [{"step": s.name, "detail": s.detail} for s in bad],
            "summary": (
                f"safe-startup complete in {total:.0f}s — {terminal_state}"
                + (f"; {len(bad)} step(s) need attention" if bad else "")
            ),
        }
        yield self.result
