"""Capture sequence orchestrator.

Takes one Planner-built target dict + its exposure_plan and walks it
to completion: for each per-filter set, change filter (if mono), kick
N exposures via NINA's camera_capture endpoint, dither between frames
through PHD2, watch for guiding loss, and surface per-frame progress.

This is the "Operator commands a sequence, NINA executes, auto-ingest
closes the loop" path that Phase 3 needed. The watch-folder ingest
(Archivist) picks up each FITS as NINA writes it, so this orchestrator
doesn't have to know anything about file paths — it just commands
exposures and the rest is plumbed.

Design notes:
  * Async-generator interface (``run()``) so the Operator's wiring is
    just a for-loop. Each yielded dict is a progress event for the
    dashboard / bus.
  * Per-frame events carry: target_name, filter, frame_index_in_set,
    frames_done_total, frames_total, elapsed_s, eta_s, state, summary.
  * Abort is cooperative — call ``seq.abort(reason)`` and the next
    poll tick stops cleanly with a "aborted" terminal event.
  * Guiding safety: between exposures we peek at the PHD2 event
    stream and pause for up to 30 s if guiding has gone bad. Real
    PHD2 emits StarLost; the FakePhd2 just returns "ok" so sim mode
    sails through.
  * Sim mode: FakeNina.camera_capture sleeps proportionally to the
    exposure (capped at 0.3 s in fakes), so the orchestrator runs
    fast enough for a 3-hour plan to complete in seconds. Useful
    for end-to-end tests; not realistic timing for UI tuning.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, AsyncIterator

from atlas.logging_setup import get_logger

log = get_logger("capture.sequence")


class CaptureSequence:
    """One full nightly target plan, executed exposure-by-exposure.

    The Operator instantiates this, attaches it to its `_current_capture`
    slot, then async-iterates over `run()` for live progress.
    """

    def __init__(self, *, nina, phd2, target: dict, exposure_plan: list[dict],
                 dither_every_n_frames: int = 1,
                 simulation: bool = False,
                 session_id: int | None = None) -> None:
        self._nina = nina
        self._phd2 = phd2
        self._target = target
        self._plan = exposure_plan or []
        self._dither_every = max(1, int(dither_every_n_frames))
        self._sim = bool(simulation)
        self._session_id = session_id
        self._abort = False
        self._abort_reason = ""
        self.frames_total = sum(int(s.get("count") or 0) for s in self._plan)
        self.frames_done = 0
        self.started_at: float | None = None

    # ---- public control ---------------------------------------------------

    def abort(self, reason: str = "operator-abort") -> None:
        """Cooperative abort. The next exposure boundary will exit
        cleanly with a `state="aborted"` terminal event."""
        self._abort = True
        self._abort_reason = reason or "abort"

    # ---- internals --------------------------------------------------------

    def _event(self, *, state: str, summary: str, **extra) -> dict:
        elapsed = (time.monotonic() - self.started_at) if self.started_at else 0.0
        eta = None
        if self.frames_done > 0 and self.frames_total > 0:
            mean_s = elapsed / self.frames_done
            eta = mean_s * (self.frames_total - self.frames_done)
        return {
            "state": state,
            "summary": summary,
            "target_name": self._target.get("target_name"),
            "frames_done": self.frames_done,
            "frames_total": self.frames_total,
            "elapsed_s": round(elapsed, 1),
            "eta_s": (round(eta, 1) if eta is not None else None),
            **extra,
        }

    async def _change_filter_if_needed(self, filter_name: str | None,
                                         current: dict) -> dict:
        """In mono mode we issue a filter-wheel change. NINA latches the
        filter into the next capture, but we do this defensively so
        ``filterwheel_info`` reflects the right slot for the dashboard.
        OSC users see this as a no-op."""
        if not filter_name or filter_name.upper() == "OSC":
            return current
        if (current.get("current_filter") or "").upper() == filter_name.upper():
            return current
        try:
            # NINA's filter change uses the camera_capture's filter param
            # in practice; we just record intent here and let the next
            # capture command supply it.
            log.info("filter change pending: %s -> %s",
                       current.get("current_filter"), filter_name)
        except Exception:
            pass
        return {**current, "current_filter": filter_name}

    async def _guiding_ok(self) -> bool:
        """Cheap reachability check. In real mode we ask PHD2 for the
        app state; sim mode always returns True."""
        if self._sim:
            return True
        try:
            state = await self._phd2.get_app_state()
        except Exception:
            return False
        return str(state).lower() in ("guiding", "settling")

    async def _wait_for_guiding(self, timeout_s: float = 30.0) -> bool:
        """Block (up to timeout) waiting for guiding to recover. Returns
        True if it came back, False if we hit the timeout."""
        if self._sim:
            return True
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await self._guiding_ok():
                return True
            await asyncio.sleep(2.0)
        return False

    async def _dither_if_due(self, frame_index_global: int) -> None:
        """PHD2 dither between exposures. Skipped in sim mode."""
        if self._sim:
            return
        if frame_index_global % self._dither_every != 0:
            return
        try:
            await self._phd2.dither(amount_px=5.0, settle_pixels=1.5,
                                       settle_time_s=10, settle_timeout_s=60)
        except Exception as e:
            log.warning("dither failed (continuing): %s", e)

    # ---- main loop --------------------------------------------------------

    async def run(self) -> AsyncIterator[dict]:
        self.started_at = time.monotonic()
        yield self._event(state="started",
                           summary=f"capturing {self.frames_total} frames "
                                    f"on {self._target.get('target_name', '?')}")

        # Filterwheel state — read once, then track locally.
        try:
            fw = await self._nina.filterwheel_info()
        except Exception:
            fw = {"current_filter": None}

        global_frame = 0
        for set_idx, set_ in enumerate(self._plan):
            if self._abort:
                break
            filt = set_.get("filter")
            exposure_s = float(set_.get("exposure_s") or 0)
            count = int(set_.get("count") or 0)
            if count <= 0:
                continue
            fw = await self._change_filter_if_needed(filt, fw)
            yield self._event(
                state="filter_set_start",
                summary=f"set {set_idx+1}/{len(self._plan)}: "
                          f"{count}x{exposure_s:.0f}s {filt}",
                filter=filt, exposure_s=exposure_s, set_count=count,
            )

            for frame_in_set in range(count):
                if self._abort:
                    yield self._event(state="aborted",
                                        summary=f"aborted: {self._abort_reason}")
                    return
                global_frame += 1

                # Pre-capture safety check: is PHD2 still guiding?
                if not await self._guiding_ok():
                    yield self._event(
                        state="guiding_paused",
                        summary="guiding lost — pausing capture, waiting "
                                  "up to 30 s for recovery",
                    )
                    if not await self._wait_for_guiding(timeout_s=30.0):
                        yield self._event(
                            state="guiding_lost",
                            summary="guiding did not recover in 30 s — "
                                      "ending sequence early",
                        )
                        return

                # Capture
                try:
                    result = await self._nina.camera_capture(
                        exposure_s=exposure_s, filter_name=filt,
                    )
                except Exception as e:
                    yield self._event(
                        state="capture_error",
                        summary=f"frame failed: {e}",
                        error=str(e),
                    )
                    # Don't give up immediately — try the next frame after
                    # a short pause. If NINA's down hard, the next call
                    # will fail the same way and we'll surface again.
                    await asyncio.sleep(2.0)
                    continue

                self.frames_done += 1
                yield self._event(
                    state="frame_captured",
                    summary=f"frame {global_frame}/{self.frames_total} "
                              f"({filt} {exposure_s:.0f}s) ok",
                    filter=filt, frame_index_in_set=frame_in_set + 1,
                    set_count=count, result=result,
                )

                # Dither between frames (skipped after the very last
                # frame to save time)
                if global_frame < self.frames_total:
                    await self._dither_if_due(global_frame)

        # Reached end of plan
        yield self._event(
            state="complete",
            summary=f"sequence finished: {self.frames_done} of "
                      f"{self.frames_total} frames captured",
        )
