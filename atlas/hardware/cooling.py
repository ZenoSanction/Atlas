"""Camera cooling-setpoint controller with stable-temp wait.

The naive approach to "cool the camera before imaging" is one NINA
call to ``camera/cool?temperature=-10`` and hope. Real CMOS/CCD bodies
take 5-15 minutes to reach setpoint, especially on warm summer nights
when the delta-T is 30°C+. Hitting the setpoint isn't enough either —
imaging right after reaching it means the first dozen frames have
visibly different dark current as the sensor finishes settling.

This controller:
  * issues the cooling command
  * polls camera temperature at ``poll_interval_s``
  * declares "stable" when |temp - target| <= ``tolerance_c`` for a
    rolling window of ``stable_window_s`` seconds
  * times out at ``max_wait_s`` (default 15 min) and returns a clear
    "couldn't reach setpoint" status rather than blocking forever
  * publishes per-tick progress to shared state so the dashboard's
    Mission Control lane can show "cooling: -8.3°C → -10.0°C, stable
    in ~45s" instead of an opaque silence

Sim mode: the FakeNina layer doesn't model thermal mass, so the
controller models it locally — exponential decay toward the setpoint
with a 90-second time constant. Lets the state machine + the
dashboard's progress bar get exercised against believable behaviour
with no hardware connected.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from atlas.logging_setup import get_logger

log = get_logger("hardware.cooling")


# Default thermal time-constant for the simulator (seconds). Real CMOS
# camera bodies are roughly 60-180 s depending on TEC power + ambient.
_SIM_TAU_S = 90.0
# Default tolerance for "we're at setpoint" decision.
_DEFAULT_TOLERANCE_C = 0.3
# How long the temperature must stay inside tolerance to be called stable.
_DEFAULT_STABLE_WINDOW_S = 60.0
# Polling cadence.
_DEFAULT_POLL_S = 5.0


# ---- result types ----------------------------------------------------------

@dataclass
class CoolingSnapshot:
    """One progress update — published every poll tick."""
    state: str                # "cooling" | "settling" | "stable" | "timeout" | "error"
    target_c: float
    current_c: Optional[float]
    delta_c: Optional[float]
    elapsed_s: float
    eta_s: Optional[float]
    samples_in_window: int
    note: str = ""

    def to_jsonable(self) -> dict:
        return asdict(self)


class CoolingError(RuntimeError):
    """Raised when the camera reports an unrecoverable error during cooling."""


# ---- the controller --------------------------------------------------------

class CoolingController:
    """Cools the camera and reports back when it's stable.

    Usage::

        ctrl = CoolingController(nina_client, target_c=-10.0)
        async for snap in ctrl.run():
            await broadcast(snap)            # progress to dashboard
        # ctrl.final has the terminal CoolingSnapshot

    The controller is an async generator so the dashboard can consume
    progress events without callbacks. The terminal snapshot is also
    stored on ``self.final`` for callers that just want the outcome.
    """

    def __init__(self, nina, *, target_c: float,
                 tolerance_c: float = _DEFAULT_TOLERANCE_C,
                 stable_window_s: float = _DEFAULT_STABLE_WINDOW_S,
                 poll_interval_s: float = _DEFAULT_POLL_S,
                 max_wait_s: float = 900.0,
                 simulation: bool = False,
                 sim_ambient_c: float = 20.0,
                 sim_tau_s: float = _SIM_TAU_S) -> None:
        self._nina = nina
        self.target_c = float(target_c)
        self.tolerance_c = float(tolerance_c)
        self.stable_window_s = float(stable_window_s)
        self.poll_interval_s = float(poll_interval_s)
        self.max_wait_s = float(max_wait_s)
        self._sim = bool(simulation)
        # Simulator state: track current temperature ourselves since
        # FakeNina just echoes the setpoint back without modelling
        # thermal mass.
        self._sim_temp = sim_ambient_c
        self._sim_tau = sim_tau_s
        # Rolling buffer of (timestamp, temp) for the stable-window check.
        self._window: deque[tuple[float, float]] = deque()
        self.final: CoolingSnapshot | None = None

    async def _read_temp(self) -> Optional[float]:
        """Get the current camera temperature. In sim mode runs an
        exponential decay; in real mode reads NINA's camera_info."""
        if self._sim:
            # Step the simulator one poll-interval forward
            dt = self.poll_interval_s
            decay = math.exp(-dt / self._sim_tau)
            self._sim_temp = (self.target_c
                                + (self._sim_temp - self.target_c) * decay)
            return self._sim_temp
        try:
            info = await self._nina.camera_info()
        except Exception as e:
            log.warning("camera_info failed during cooling: %s", e)
            return None
        if not isinstance(info, dict):
            return None
        t = info.get("temperature")
        try:
            return float(t) if t is not None else None
        except (TypeError, ValueError):
            return None

    def _check_stable(self, now: float) -> bool:
        """True if every sample in the rolling window is within tolerance.

        "Rolling window" = the last ``stable_window_s`` seconds of samples.
        Pruning removes anything older than that, so by construction the
        oldest sample's age is < window. We declare stable when:
          - every sample currently in the window is inside tolerance, AND
          - we have enough samples to span the window at the poll rate
            (i.e. we've actually been observing for the full window, not
            just two ticks ago)
        """
        cutoff = now - self.stable_window_s
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()
        # Need enough samples to span the full window at the poll cadence.
        # We accept slightly less than the theoretical maximum to absorb
        # timing jitter (sleep ≠ exact poll_interval).
        required = max(3, int(self.stable_window_s / self.poll_interval_s) - 1)
        if len(self._window) < required:
            return False
        return all(abs(t - self.target_c) <= self.tolerance_c
                     for _, t in self._window)

    async def _issue_setpoint(self) -> None:
        try:
            await self._nina.camera_set_cooling(target_c=self.target_c)
        except Exception as e:
            raise CoolingError(f"NINA cooling command failed: {e}") from e

    def _estimate_eta(self, now: float, start: float,
                       current: float | None) -> Optional[float]:
        """Rough ETA using exponential-decay assumption. Adequate for a
        progress display; not a guarantee."""
        if current is None or len(self._window) < 2:
            return None
        delta = abs(current - self.target_c)
        if delta <= self.tolerance_c:
            return self.stable_window_s
        # Fit τ from the last two samples
        t0, T0 = self._window[0]
        if t0 == now:
            return None
        try:
            ratio = (T0 - self.target_c) / (current - self.target_c)
            if ratio <= 1.0:
                return None
            tau = (now - t0) / math.log(ratio)
        except (ValueError, ZeroDivisionError):
            return None
        # Time to drop |delta| → tolerance
        remaining = tau * math.log(delta / self.tolerance_c)
        return max(0.0, remaining + self.stable_window_s)

    async def run(self):
        """Async generator yielding CoolingSnapshot progress events.
        Final snapshot is also stored on ``self.final``."""
        await self._issue_setpoint()
        start = time.monotonic()
        while True:
            current = await self._read_temp()
            now = time.monotonic()
            elapsed = now - start
            if current is not None:
                self._window.append((now, current))
            delta = (current - self.target_c) if current is not None else None
            eta = self._estimate_eta(now, start, current)

            if elapsed > self.max_wait_s:
                snap = CoolingSnapshot(
                    state="timeout", target_c=self.target_c, current_c=current,
                    delta_c=delta, elapsed_s=round(elapsed, 1), eta_s=None,
                    samples_in_window=len(self._window),
                    note=f"Reached {elapsed:.0f}s without stabilising "
                          f"within ±{self.tolerance_c:.1f}°C.",
                )
                self.final = snap
                yield snap
                return

            stable = self._check_stable(now)
            if stable:
                snap = CoolingSnapshot(
                    state="stable", target_c=self.target_c, current_c=current,
                    delta_c=delta, elapsed_s=round(elapsed, 1), eta_s=0.0,
                    samples_in_window=len(self._window),
                    note=f"Stable at {current:.2f}°C "
                          f"(within ±{self.tolerance_c:.1f}°C for "
                          f"{self.stable_window_s:.0f}s).",
                )
                self.final = snap
                yield snap
                return

            # Still working — emit progress and keep going
            if current is not None and abs(delta) <= self.tolerance_c:
                state = "settling"
                note = (f"Inside tolerance at {current:.2f}°C — "
                          f"holding for stable window…")
            else:
                state = "cooling"
                note = (f"Cooling: {current:.2f}°C → {self.target_c:.1f}°C "
                          f"(Δ {abs(delta):.1f}°C)") if current is not None \
                       else "Awaiting camera temperature…"
            yield CoolingSnapshot(
                state=state, target_c=self.target_c, current_c=current,
                delta_c=delta, elapsed_s=round(elapsed, 1),
                eta_s=(round(eta, 1) if eta is not None else None),
                samples_in_window=len(self._window), note=note,
            )
            await asyncio.sleep(self.poll_interval_s)


async def cool_to_setpoint(nina, *, target_c: float,
                            tolerance_c: float = _DEFAULT_TOLERANCE_C,
                            max_wait_s: float = 900.0,
                            simulation: bool = False,
                            on_progress=None) -> CoolingSnapshot:
    """Convenience wrapper: run a controller to completion and return
    the final snapshot. ``on_progress`` is an optional async callback
    invoked with each progress event (for broadcasting to the bus)."""
    ctrl = CoolingController(nina, target_c=target_c, tolerance_c=tolerance_c,
                                max_wait_s=max_wait_s, simulation=simulation)
    async for snap in ctrl.run():
        if on_progress is not None:
            try:
                await on_progress(snap)
            except Exception:
                log.exception("cooling on_progress callback failed")
    return ctrl.final
