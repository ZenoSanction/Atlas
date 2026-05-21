"""Quality grading for ingested frames.

Two-stage grader:
  1. Prefer FWHM from the FITS header (NINA, Voyager, SGP all write it).
     Classify by site-relative tertile.
  2. Fall back to numpy-only star count: threshold + connected-component
     blob count. Very cheap, good enough for "did this exposure see stars
     at all?" gradings (light vs. closed-shutter dark).

Grades:
  A  — excellent (FWHM in best 25%, or ≥ 50 detections for lights)
  B  — good, science-ready (FWHM 25-75 %ile, or ≥ 20 detections)
  C  — marginal (FWHM in worst 25%, or 5-20 detections)
  D  — discard (FWHM > critical_arcsec, or < 5 detections on a light)
  UNGRADED  — couldn't open / no data / non-light frame

Calibration frames (bias/dark/flat) are always graded "A" — meaningful
quality assessment for those needs different stats.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from atlas.db.session import get_session
from atlas.db.models import Frame, FrameQuality
from atlas.logging_setup import get_logger

log = get_logger("capture.quality")


# FWHM thresholds in arcseconds (rough amateur defaults). Adjust per site
# in a future commit; for now, a single global cutoff set.
_FWHM_A_MAX = 2.5
_FWHM_B_MAX = 4.0
_FWHM_C_MAX = 6.0
# above C_MAX -> D


def _count_blobs(data) -> int:
    """Quick-and-dirty star count: threshold at median + 5*MAD, label
    connected components, return component count.  Pure numpy + no scipy
    dependency by using a simple flood fill."""
    try:
        import numpy as np
    except ImportError:
        return 0
    if data is None or data.size == 0:
        return 0
    arr = data.astype("float32", copy=False)
    # Robust threshold
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med))) or 1.0
    thr = med + 5.0 * 1.4826 * mad
    mask = arr > thr
    if not mask.any():
        return 0
    # 4-connected flood fill via a simple stack
    visited = np.zeros_like(mask, dtype=bool)
    count = 0
    H, W = mask.shape
    for sy in range(H):
        for sx in range(W):
            if not mask[sy, sx] or visited[sy, sx]:
                continue
            count += 1
            stack = [(sy, sx)]
            while stack:
                y, x = stack.pop()
                if y < 0 or y >= H or x < 0 or x >= W:
                    continue
                if visited[y, x] or not mask[y, x]:
                    continue
                visited[y, x] = True
                stack.extend([(y+1, x), (y-1, x), (y, x+1), (y, x-1)])
            if count >= 200:
                # don't waste time counting every component for crowded fields
                return count
    return count


def _grade_from_fwhm(fwhm: float) -> str:
    if fwhm <= _FWHM_A_MAX:
        return "A"
    if fwhm <= _FWHM_B_MAX:
        return "B"
    if fwhm <= _FWHM_C_MAX:
        return "C"
    return "D"


def _grade_from_stars(n: int) -> str:
    if n >= 50:
        return "A"
    if n >= 20:
        return "B"
    if n >= 5:
        return "C"
    return "D"


def grade_frame(frame_id: int) -> dict:
    """Grade one frame by id. Writes back to frames.quality. Returns a
    dict with the grade + which method produced it."""
    with get_session() as s:
        f = s.get(Frame, frame_id)
        if f is None:
            return {"error": f"frame {frame_id} not found"}
        # Calibration frames don't get FWHM grading
        if f.frame_type in ("bias", "dark", "flat"):
            f.quality = FrameQuality.A
            return {"frame_id": frame_id, "grade": "A",
                    "method": "calibration-default"}
        # Prefer header FWHM if present
        fwhm = f.fwhm_arcsec
        path = f.file_path
    if fwhm is not None and fwhm > 0:
        grade = _grade_from_fwhm(float(fwhm))
        method = f"fwhm-from-header({fwhm:.2f}\")"
    else:
        # Fall back to numpy star count
        try:
            from astropy.io import fits
            import numpy as np
            with fits.open(path, memmap=False) as hdul:
                data = hdul[0].data
        except Exception as e:
            return {"frame_id": frame_id, "error": f"read failed: {e}"}
        n = _count_blobs(data)
        grade = _grade_from_stars(n)
        method = f"blob-count({n})"
    # Persist
    with get_session() as s:
        f = s.get(Frame, frame_id)
        if f is None:
            return {"error": "frame disappeared"}
        f.quality = FrameQuality(grade)
    log.info("Graded frame #%d → %s (%s)", frame_id, grade, method)
    return {"frame_id": frame_id, "grade": grade, "method": method}


def grade_ungraded(limit: int = 100) -> list[dict]:
    """Walk recent ungraded frames and grade them in bulk."""
    with get_session() as s:
        rows = (s.query(Frame.id)
                  .filter(Frame.quality == FrameQuality.UNGRADED)
                  .order_by(Frame.id.desc())
                  .limit(limit).all())
        ids = [r[0] for r in rows]
    return [grade_frame(i) for i in ids]
