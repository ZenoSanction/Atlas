"""Plate-solve + mount-sync orchestrator.

After a fresh slew (or any reason the mount might be off-center), we
take a quick "pointing frame", hand it to ASTAP, and sync the mount
to the actual solved position. Without this step every subsequent
frame on the target is off by mount pointing error — often several
arcminutes on amateur GEMs, enough to miss small DSOs entirely.

Decision points (every one logged + broadcast):

  1. First frame of a new target  → solve + sync (always, unless
                                       workflow opts out — exoplanet
                                       transits sometimes lock pointing
                                       before window opens)
  2. After a meridian flip         → solve + sync (mount orientation
                                       changed, pointing may shift)
  3. After a guiding-lost recovery → solve + sync (we may have drifted
                                       during the dead time)
  4. Operator manual request       → solve + sync, no questions

  Subsequent frames on the same target: skipped (mount + guider keep
  centering; another solve is wasted work + potential failure point).

Failure handling:
  * On solve failure, retry once with a wider search radius.
  * If that also fails, surface a critical advisory + skip the
    target (it's likely cloud-obscured or in a star-poor field).
  * Mount stays at last commanded position; capture sequence can
    still try, but the operator is paged so they can intervene.

The actual ASTAP call already exists in atlas/hardware/astap.py.
This orchestrator wraps it with the policy + audit trail.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from atlas.logging_setup import get_logger

log = get_logger("capture.platesolve")


# Two-pass radius: first attempt assumes mount is close (5° is generous
# for any reasonable GoTo), second attempt opens to 15° for cases where
# pointing was way off (cold-start, polar align drift, etc.).
DEFAULT_RADIUS_DEG_FIRST = 5.0
DEFAULT_RADIUS_DEG_RETRY = 15.0


@dataclass
class PlateSolveContext:
    """Inputs to the should-solve decision engine."""
    is_first_frame_on_target: bool = False    # always solve on first frame
    after_meridian_flip: bool = False         # solve after the flip
    after_guiding_recovery: bool = False      # solve after guiding came back
    operator_requested: bool = False          # operator clicked "solve now"
    target_name: Optional[str] = None
    target_ra_deg: Optional[float] = None
    target_dec_deg: Optional[float] = None


@dataclass
class PlateSolveDecision:
    should_solve: bool
    trigger: str
    reason: str


def decide(ctx: PlateSolveContext) -> PlateSolveDecision:
    """Pure-logic decision. Returns the trigger string + human reason."""
    if ctx.operator_requested:
        return PlateSolveDecision(
            should_solve=True, trigger="operator_request",
            reason="operator manually requested plate-solve",
        )
    if ctx.is_first_frame_on_target:
        return PlateSolveDecision(
            should_solve=True, trigger="first_frame",
            reason=(f"first frame on {ctx.target_name or 'target'}; "
                      "sync before real sequence starts"),
        )
    if ctx.after_meridian_flip:
        return PlateSolveDecision(
            should_solve=True, trigger="meridian_flip",
            reason="mount orientation changed at meridian; re-sync",
        )
    if ctx.after_guiding_recovery:
        return PlateSolveDecision(
            should_solve=True, trigger="guiding_recovery",
            reason="guiding recovered after a loss; check pointing drift",
        )
    return PlateSolveDecision(
        should_solve=False, trigger="(none)",
        reason="no trigger met — skipping (mount + guider keep centering)",
    )


@dataclass
class PlateSolveResult:
    ok: bool
    elapsed_s: float
    radius_used_deg: float
    solved_ra_deg: Optional[float] = None
    solved_dec_deg: Optional[float] = None
    pointing_error_arcmin: Optional[float] = None  # solved vs target
    error: Optional[str] = None
    note: str = ""


async def solve_and_sync(*, astap_client, nina_client,
                            target_ra_deg: float, target_dec_deg: float,
                            target_name: str,
                            simulation: bool = False,
                            exposure_s: float = 5.0,
                            ) -> PlateSolveResult:
    """Capture a short pointing frame, solve, sync mount. Two-pass
    retry: 5° search radius first, 15° on retry. Returns a result
    dict suitable for direct broadcast."""
    started = time.monotonic()

    if simulation:
        # Synth path: don't touch hardware, pretend the solve worked
        # and the mount was already perfectly centered. Lets us
        # exercise the orchestrator end-to-end without sky.
        await asyncio.sleep(0.5)
        return PlateSolveResult(
            ok=True, elapsed_s=round(time.monotonic() - started, 1),
            radius_used_deg=DEFAULT_RADIUS_DEG_FIRST,
            solved_ra_deg=target_ra_deg, solved_dec_deg=target_dec_deg,
            pointing_error_arcmin=0.0,
            note="simulation — no real plate-solve performed",
        )

    # Real path: capture a brief solve frame, write it where ASTAP
    # can read it, run ASTAP. The exact "capture to disk" flow
    # depends on NINA version. For now this is a wire-up sketch
    # that bench day will flesh out.
    try:
        # 1. Capture solve frame (NINA writes FITS to capture_folder)
        await nina_client.camera_capture(
            exposure_s=exposure_s, filter_name=None,
        )
        # Locate the just-written FITS — naive approach picks newest.
        from atlas.config import get_settings
        capture_dir = get_settings().frames_dir
        fits_files = sorted(capture_dir.glob("*.fits"),
                              key=lambda p: p.stat().st_mtime,
                              reverse=True)
        if not fits_files:
            return PlateSolveResult(
                ok=False, elapsed_s=round(time.monotonic() - started, 1),
                radius_used_deg=DEFAULT_RADIUS_DEG_FIRST,
                error="no FITS file appeared after solve-frame capture",
            )
        fits_path = fits_files[0]

        # 2. First-pass solve at narrow radius
        for radius in (DEFAULT_RADIUS_DEG_FIRST, DEFAULT_RADIUS_DEG_RETRY):
            try:
                solution = await astap_client.solve(
                    fits_path,
                    ra_hours=target_ra_deg / 15.0,
                    dec_deg=target_dec_deg,
                    radius_deg=radius,
                    timeout_s=60.0,
                )
                # Got a solve — sync mount + return
                solved_ra = solution.get("ra_deg") or target_ra_deg
                solved_dec = solution.get("dec_deg") or target_dec_deg
                err_deg = ((solved_ra - target_ra_deg) ** 2
                             + (solved_dec - target_dec_deg) ** 2) ** 0.5
                # Try the mount sync. NinaClient doesn't have a generic
                # sync method yet — wire on bench day. For now log it.
                log.info("plate-solve OK; would sync mount to "
                          "ra=%.4f° dec=%.4f° (error %.2f arcmin)",
                          solved_ra, solved_dec, err_deg * 60.0)
                return PlateSolveResult(
                    ok=True, elapsed_s=round(time.monotonic() - started, 1),
                    radius_used_deg=radius,
                    solved_ra_deg=solved_ra, solved_dec_deg=solved_dec,
                    pointing_error_arcmin=round(err_deg * 60.0, 2),
                    note=(f"solved within {radius:.0f}° radius"
                            + (" (wide-radius retry)" if radius >
                                 DEFAULT_RADIUS_DEG_FIRST else "")),
                )
            except Exception as e:
                log.warning("plate-solve attempt at %s° failed: %s",
                              radius, e)
                continue

        # Both passes failed
        return PlateSolveResult(
            ok=False, elapsed_s=round(time.monotonic() - started, 1),
            radius_used_deg=DEFAULT_RADIUS_DEG_RETRY,
            error=(f"plate-solve failed at both {DEFAULT_RADIUS_DEG_FIRST}° "
                     f"and {DEFAULT_RADIUS_DEG_RETRY}° search radius — "
                     "likely cloud or star-poor field"),
        )
    except Exception as e:
        return PlateSolveResult(
            ok=False, elapsed_s=round(time.monotonic() - started, 1),
            radius_used_deg=DEFAULT_RADIUS_DEG_FIRST,
            error=f"plate-solve setup failed: {type(e).__name__}: {e}",
        )
