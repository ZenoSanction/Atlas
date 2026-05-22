"""Twilight flat orchestrator.

Sky flats are captured during the brief window when the sun is below
the horizon but the sky is still bright enough to flood the sensor
evenly. That window is roughly:

  Evening: sun altitude -3 deg (sky bright)   -> -8 deg (sky too dim)
  Morning: sun altitude -8 deg (sky too dim)  -> -3 deg (sky too bright)

Inside the window, sky brightness halves about every degree the sun
sets, so the orchestrator continuously re-tunes exposure to keep
the mean ADU at our target (~30,000 of 65,535 for a 16-bit camera).

Per-filter sequencing: faster filters (L, OSC) first when sky is
darkening (evening) — they'll be exposing seconds; narrowbands last.
Order is reversed for morning flats. Adapting per-filter exposure
on the fly is what makes this reliable across changing sky brightness.

Acceptance per frame:
  * mean_adu within [TARGET_ADU - 30%, TARGET_ADU + 30%]
  * out-of-band frames are flagged (the human can decide to discard
    or accept; we don't auto-reject because sometimes you want them)

This module is the *orchestration* layer only — the per-exposure ADU
measurement reads back from the FITS the camera just wrote. Bench
day wires that to the real NINA + FITS path; sim mode synthesizes
ADU values that track sun altitude so the orchestrator can be
exercised end-to-end.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import AsyncIterator, Optional

from atlas.logging_setup import get_logger

log = get_logger("calibration.twilight_flats")


# Tuning knobs the dashboard can later expose to the operator.
TARGET_ADU = 30000.0          # half the 16-bit dynamic range
ADU_TOLERANCE_PCT = 30.0       # acceptable band around TARGET_ADU (±%)
MIN_EXPOSURE_S = 0.5           # mechanical shutter / electronic min
MAX_EXPOSURE_S = 30.0          # past this, sky is too dim — bail
INITIAL_TEST_EXPOSURE_S = 1.0  # first probe per filter
N_FRAMES_PER_FILTER = 10       # standard flat stack count

# Evening window: orchestrator opens at -3 deg, closes at -8 deg.
EVENING_START_SUN_ALT = -3.0
EVENING_STOP_SUN_ALT = -8.0
# Morning is reversed: opens at -8 deg, closes at -3 deg.
MORNING_START_SUN_ALT = -8.0
MORNING_STOP_SUN_ALT = -3.0


@dataclass
class FlatCaptureResult:
    """One captured flat frame's outcome."""
    filter_name: str
    exposure_s: float
    mean_adu: Optional[float] = None
    accepted: bool = False
    note: str = ""


@dataclass
class TwilightFlatPlan:
    """The orchestrator's pre-flight plan: filter order + when the
    window opens/closes for tonight's site + sun trajectory."""
    direction: str               # "evening" | "morning"
    filter_order: list[str]
    window_start_utc: Optional[datetime]
    window_stop_utc: Optional[datetime]
    summary: str = ""


# ---- planner ------------------------------------------------------------

def _scan_for_crossing(latitude_deg: float, longitude_deg: float,
                          search_start: datetime, search_end: datetime,
                          target_alt_deg: float,
                          descending: bool,
                          step_min: int = 20) -> Optional[datetime]:
    """Walk ``step_min``-minute slices from search_start to search_end
    looking for the first slice whose two endpoints bracket
    target_alt_deg in the requested direction, then binary-search
    inside that slice via _find_sun_crossing.

    The underlying _find_sun_crossing only works on a single
    monotonic bracket — a 24h window crosses any sub-horizon
    altitude twice and confuses it. Pre-scanning narrows the
    bracket to one transition."""
    from atlas.astronomy.visibility import (
        _find_sun_crossing, sun_altitude,
    )
    cursor = search_start
    last_alt = sun_altitude(latitude_deg, longitude_deg, cursor)
    while cursor < search_end:
        nxt = cursor + timedelta(minutes=step_min)
        if nxt > search_end:
            nxt = search_end
        next_alt = sun_altitude(latitude_deg, longitude_deg, nxt)
        crossed = False
        if descending:
            crossed = (last_alt > target_alt_deg and next_alt < target_alt_deg)
        else:
            crossed = (last_alt < target_alt_deg and next_alt > target_alt_deg)
        if crossed:
            return _find_sun_crossing(latitude_deg, longitude_deg,
                                          cursor, nxt, target_alt_deg,
                                          descending=descending)
        cursor = nxt
        last_alt = next_alt
    return None


def plan_window(*, direction: str, when_utc: datetime,
                  latitude_deg: float, longitude_deg: float,
                  filter_order: list[str]) -> TwilightFlatPlan:
    """Look up when the twilight-flat window opens/closes for the
    given site + UT day. ``direction`` is 'evening' (sun descending
    through -3 -> -8) or 'morning' (sun ascending through -8 -> -3)."""
    # Search +24h forward from when_utc. The scanner narrows brackets
    # automatically, so we don't have to know the exact sunset time.
    search_start = when_utc
    search_end = when_utc + timedelta(hours=24)
    if direction == "evening":
        # Sun descending: -3 first (start), then -8 (stop)
        start_utc = _scan_for_crossing(
            latitude_deg, longitude_deg, search_start, search_end,
            EVENING_START_SUN_ALT, descending=True)
        # Stop crossing must be after the start crossing
        stop_search_start = start_utc or search_start
        stop_utc = _scan_for_crossing(
            latitude_deg, longitude_deg, stop_search_start, search_end,
            EVENING_STOP_SUN_ALT, descending=True)
        ordered = filter_order   # broadband first; narrowband last
    elif direction == "morning":
        # Sun ascending: -8 first (start), then -3 (stop)
        start_utc = _scan_for_crossing(
            latitude_deg, longitude_deg, search_start, search_end,
            MORNING_START_SUN_ALT, descending=False)
        stop_search_start = start_utc or search_start
        stop_utc = _scan_for_crossing(
            latitude_deg, longitude_deg, stop_search_start, search_end,
            MORNING_STOP_SUN_ALT, descending=False)
        # Morning: start dim and brightening — narrowband first
        ordered = list(reversed(filter_order))
    else:
        raise ValueError(f"direction must be 'evening' or 'morning'; "
                            f"got {direction!r}")
    duration_min: Optional[float] = None
    if start_utc and stop_utc:
        duration_min = (stop_utc - start_utc).total_seconds() / 60.0
    summary = (
        f"{direction} flat window: "
        f"{start_utc.strftime('%H:%M') if start_utc else '?'} -> "
        f"{stop_utc.strftime('%H:%M') if stop_utc else '?'} UTC "
        f"({duration_min:.0f} min)" if duration_min is not None
        else f"{direction} flat window not found in 24h search"
    )
    return TwilightFlatPlan(
        direction=direction, filter_order=ordered,
        window_start_utc=start_utc, window_stop_utc=stop_utc,
        summary=summary,
    )


# ---- orchestrator -------------------------------------------------------

class TwilightFlatOrchestrator:
    """Walks the twilight-flat window, capturing N flats per filter
    with per-frame exposure re-tuning. Yields per-step events so
    the human sees each decision (exposure adjusted from X to Y,
    ADU measured at Z, frame accepted/rejected)."""

    def __init__(self, *, nina, latitude_deg: float, longitude_deg: float,
                   direction: str = "evening",
                   filter_order: list[str] | None = None,
                   n_frames_per_filter: int = N_FRAMES_PER_FILTER,
                   target_adu: float = TARGET_ADU,
                   adu_tolerance_pct: float = ADU_TOLERANCE_PCT,
                   simulation: bool = False) -> None:
        self._nina = nina
        self._lat = float(latitude_deg)
        self._lon = float(longitude_deg)
        self._direction = direction
        self._filter_order = filter_order or ["L", "R", "G", "B"]
        self._n_per = int(n_frames_per_filter)
        self._target_adu = float(target_adu)
        self._tol_pct = float(adu_tolerance_pct)
        self._sim = bool(simulation)
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    # ---- per-frame helpers ----

    async def _capture_flat(self, *, filter_name: str,
                              exposure_s: float) -> dict:
        """Issue one flat-frame capture command + read back its mean ADU.
        Real path: NINA camera_capture(frame_type='flat'). Sim path:
        synth ADU based on sun altitude + a small jitter."""
        if self._sim:
            from atlas.astronomy.visibility import sun_altitude
            alt = sun_altitude(self._lat, self._lon, datetime.utcnow())
            # Sky brightness ∝ exp(k*alt). Pick k so a 1s exposure at
            # alt=-5 gives ~30k ADU. Real instruments differ — this is
            # a synthetic stand-in for testing the orchestration.
            scale = math.exp(0.6 * (alt + 5.0))
            mean_adu = scale * exposure_s * 30000.0
            # Add 5% jitter
            mean_adu *= 1.0 + 0.05 * (math.sin(time.monotonic()) * 0.5)
            return {"ok": True, "mean_adu": mean_adu,
                      "exposure_s": exposure_s, "filter": filter_name}
        # Real path:
        result = await self._nina.camera_capture(
            exposure_s=exposure_s, filter_name=filter_name,
            frame_type="flat",
        )
        return result

    def _adjust_exposure(self, *, current_exposure: float,
                            measured_adu: float) -> float:
        """Multiply current exposure by (target / measured), clamped."""
        if measured_adu is None or measured_adu <= 0:
            return current_exposure
        ratio = self._target_adu / measured_adu
        new_exp = current_exposure * ratio
        return max(MIN_EXPOSURE_S, min(MAX_EXPOSURE_S, new_exp))

    def _frame_accepted(self, mean_adu: Optional[float]) -> bool:
        if mean_adu is None:
            return False
        lo = self._target_adu * (1.0 - self._tol_pct / 100.0)
        hi = self._target_adu * (1.0 + self._tol_pct / 100.0)
        return lo <= mean_adu <= hi

    def _window_still_open(self) -> bool:
        from atlas.astronomy.visibility import sun_altitude
        alt = sun_altitude(self._lat, self._lon, datetime.utcnow())
        if self._direction == "evening":
            # Open while alt is between -3 (start) and -8 (stop)
            return EVENING_STOP_SUN_ALT <= alt <= EVENING_START_SUN_ALT
        # morning
        return MORNING_START_SUN_ALT <= alt <= MORNING_STOP_SUN_ALT

    # ---- main loop ----

    async def run(self) -> AsyncIterator[dict]:
        yield {
            "phase": "started",
            "direction": self._direction,
            "filter_order": list(self._filter_order),
            "summary": (f"twilight flats starting ({self._direction}); "
                         f"filters: {','.join(self._filter_order)}"),
        }
        results: list[FlatCaptureResult] = []
        for filt in self._filter_order:
            if self._abort:
                yield {"phase": "aborted",
                         "summary": "operator aborted twilight flats"}
                return
            if not self._window_still_open():
                yield {"phase": "window_closed",
                         "summary": ("twilight window closed before all "
                                       "filters captured — stopping early"),
                         "filters_completed":
                             [r.filter_name for r in results
                                if r.accepted],
                         }
                break
            # First probe shot
            yield {"phase": "filter_started", "filter": filt,
                     "summary": f"starting filter {filt} — probe exposure "
                                  f"{INITIAL_TEST_EXPOSURE_S:.1f}s"}
            probe = await self._capture_flat(filter_name=filt,
                                                 exposure_s=INITIAL_TEST_EXPOSURE_S)
            exposure_s = self._adjust_exposure(
                current_exposure=INITIAL_TEST_EXPOSURE_S,
                measured_adu=probe.get("mean_adu"),
            )
            yield {"phase": "exposure_tuned", "filter": filt,
                     "from_s": INITIAL_TEST_EXPOSURE_S,
                     "to_s": round(exposure_s, 2),
                     "probe_adu": probe.get("mean_adu"),
                     "summary": (f"{filt}: probe ADU "
                                   f"{probe.get('mean_adu'):.0f} -> tuned "
                                   f"exposure to {exposure_s:.2f}s")}
            # Sanity: if tuned exposure is at the floor (sky too bright)
            # or ceiling (sky too dim), skip this filter
            if exposure_s <= MIN_EXPOSURE_S * 1.05:
                results.append(FlatCaptureResult(
                    filter_name=filt, exposure_s=exposure_s,
                    accepted=False,
                    note="sky too bright — tuned exposure hit min floor"))
                yield {"phase": "filter_skipped", "filter": filt,
                         "reason": "sky too bright",
                         "summary": (f"{filt}: skipped — sky too bright "
                                       f"(would need < {MIN_EXPOSURE_S}s)")}
                continue
            if exposure_s >= MAX_EXPOSURE_S * 0.95:
                results.append(FlatCaptureResult(
                    filter_name=filt, exposure_s=exposure_s,
                    accepted=False,
                    note="sky too dim — tuned exposure hit max ceiling"))
                yield {"phase": "filter_skipped", "filter": filt,
                         "reason": "sky too dim",
                         "summary": (f"{filt}: skipped — sky too dim "
                                       f"(would need > {MAX_EXPOSURE_S}s)")}
                continue

            # Capture N frames, re-tuning exposure between each
            accepted_n = 0
            for i in range(self._n_per):
                if self._abort:
                    break
                if not self._window_still_open():
                    yield {"phase": "window_closed", "filter": filt,
                             "summary": (f"{filt}: window closed at frame "
                                           f"{i+1}/{self._n_per}")}
                    break
                shot = await self._capture_flat(filter_name=filt,
                                                    exposure_s=exposure_s)
                mean_adu = shot.get("mean_adu")
                accepted = self._frame_accepted(mean_adu)
                result = FlatCaptureResult(
                    filter_name=filt, exposure_s=exposure_s,
                    mean_adu=mean_adu, accepted=accepted,
                    note=("OK" if accepted else
                            f"ADU {mean_adu:.0f} out of tolerance band"),
                )
                results.append(result)
                if accepted:
                    accepted_n += 1
                yield {"phase": ("flat_accepted" if accepted
                                    else "flat_rejected"),
                         "filter": filt,
                         "frame_index": i + 1,
                         "frames_total": self._n_per,
                         "exposure_s": round(exposure_s, 2),
                         "mean_adu": (round(mean_adu, 0)
                                         if mean_adu is not None else None),
                         "summary": (f"{filt} {i+1}/{self._n_per}: "
                                       f"ADU {mean_adu:.0f} "
                                       f"({'OK' if accepted else 'out of band'})")}
                # Re-tune exposure for next shot
                exposure_s = self._adjust_exposure(
                    current_exposure=exposure_s, measured_adu=mean_adu,
                )
                # Bail if tuning has pushed us out of bounds
                if exposure_s <= MIN_EXPOSURE_S * 1.05:
                    yield {"phase": "filter_stopped_early", "filter": filt,
                             "summary": (f"{filt}: stopping early — sky now "
                                           "too bright (exposure at floor)")}
                    break
                if exposure_s >= MAX_EXPOSURE_S * 0.95:
                    yield {"phase": "filter_stopped_early", "filter": filt,
                             "summary": (f"{filt}: stopping early — sky now "
                                           "too dim (exposure at ceiling)")}
                    break

            yield {"phase": "filter_complete", "filter": filt,
                     "accepted": accepted_n,
                     "total": self._n_per,
                     "summary": (f"{filt}: {accepted_n}/{self._n_per} "
                                   "frames accepted")}

        total_accepted = sum(1 for r in results if r.accepted)
        yield {"phase": "complete",
                 "total_accepted": total_accepted,
                 "total_captured": len(results),
                 "filters_attempted": list(self._filter_order),
                 "summary": (f"twilight flats done — {total_accepted}/"
                               f"{len(results)} accepted")}
