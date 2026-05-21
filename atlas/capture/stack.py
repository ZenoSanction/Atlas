"""Master frame stacking — pure numpy median combine.

Phase-2 pass-2 ingestion utility: take N registered bias/dark/flat frames
of matching parameters and produce a single master FITS that ATLAS
auto-registers in ``calibration_masters``.

We use median-stacking (not mean) because it's robust to cosmic-ray hits
and outlier-rejection-free. For 50+ frames Sigma-clipped mean would be
marginally cleaner; for the bench-test scale of 10-30 frames the
difference is negligible.

Sample call (from Archivist tool):
    stack_master(
        kind="dark",
        exposure_s=300, ccd_temp_c=-10, gain=100,
        output_dir="C:/ATLAS/data/masters",
    )
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from atlas.db.session import get_session
from atlas.db.models import Frame, CalibrationMaster
from atlas.logging_setup import get_logger

log = get_logger("capture.stack")


# Tolerance for matching frames — small floats can differ in the 6th
# decimal place between NINA captures; treat them as "the same" within
# these windows.
_EXP_TOL_S = 0.05
_TEMP_TOL_C = 0.5


def _matches(frame: Frame, *,
              kind: str, filter_name: str | None,
              exposure_s: float | None, ccd_temp_c: float | None,
              gain: int | None, offset: int | None) -> bool:
    if frame.frame_type != kind:
        return False
    if filter_name is not None and (frame.filter_name or "") != filter_name:
        return False
    if exposure_s is not None and frame.exposure_s is not None:
        if abs(float(frame.exposure_s) - exposure_s) > _EXP_TOL_S:
            return False
    if ccd_temp_c is not None and frame.ccd_temp_c is not None:
        if abs(float(frame.ccd_temp_c) - ccd_temp_c) > _TEMP_TOL_C:
            return False
    if gain is not None and frame.gain is not None and frame.gain != gain:
        return False
    if offset is not None and frame.offset is not None and frame.offset != offset:
        return False
    return True


def find_matching_frames(*, kind: str, filter_name: str | None = None,
                           exposure_s: float | None = None,
                           ccd_temp_c: float | None = None,
                           gain: int | None = None,
                           offset: int | None = None,
                           limit: int = 200) -> list[Frame]:
    """Pull frames from the DB that match the given calibration spec."""
    with get_session() as s:
        q = s.query(Frame).filter(Frame.frame_type == kind)
        if filter_name is not None:
            q = q.filter(Frame.filter_name == filter_name)
        if gain is not None:
            q = q.filter(Frame.gain == gain)
        if offset is not None:
            q = q.filter(Frame.offset == offset)
        rows = q.order_by(Frame.captured_at.desc()).limit(limit).all()
        for r in rows:
            s.expunge(r)
    # Final tolerance filter for floats
    return [f for f in rows if _matches(
        f, kind=kind, filter_name=filter_name, exposure_s=exposure_s,
        ccd_temp_c=ccd_temp_c, gain=gain, offset=offset,
    )]


def stack_master(*, kind: str,
                   filter_name: str | None = None,
                   exposure_s: float | None = None,
                   ccd_temp_c: float | None = None,
                   gain: int | None = None,
                   offset: int | None = None,
                   output_dir: Path | str | None = None,
                   min_frames: int = 5) -> dict:
    """Median-stack matching frames into a master, write it to disk, and
    register it in calibration_masters.

    Returns a dict with master_id, file_path, n_frames, params, or error.
    """
    if kind not in ("bias", "dark", "flat"):
        return {"error": f"kind must be bias/dark/flat, got {kind!r}"}

    frames = find_matching_frames(
        kind=kind, filter_name=filter_name, exposure_s=exposure_s,
        ccd_temp_c=ccd_temp_c, gain=gain, offset=offset,
    )
    if len(frames) < min_frames:
        return {"error": (f"Only {len(frames)} matching {kind} frame(s) "
                          f"found; need at least {min_frames}. Use looser "
                          "params or capture more sub-frames first.")}

    try:
        import numpy as np
        from astropy.io import fits
    except ImportError as e:
        return {"error": f"numpy + astropy required: {e}"}

    # Load each FITS into a stack
    log.info("Stacking %d %s frame(s)...", len(frames), kind)
    cube_list = []
    ref_header = None
    for f in frames:
        try:
            with fits.open(f.file_path, memmap=False) as hdul:
                data = hdul[0].data.astype(np.float32, copy=False)
                if ref_header is None:
                    ref_header = hdul[0].header.copy()
                cube_list.append(data)
        except Exception as e:
            log.warning("Skipping %s: %s", f.file_path, e)

    if len(cube_list) < min_frames:
        return {"error": (f"After load errors only {len(cube_list)} "
                          f"frames remain; need {min_frames}.")}

    # All frames must be the same shape — drop any that differ from the
    # majority shape.
    from collections import Counter
    shape_counts = Counter(arr.shape for arr in cube_list)
    target_shape = shape_counts.most_common(1)[0][0]
    cube_list = [a for a in cube_list if a.shape == target_shape]

    cube = np.stack(cube_list, axis=0)
    log.info("Stacking cube shape %s -> median", cube.shape)
    master = np.median(cube, axis=0).astype(np.float32)

    # Build output path
    out_dir = Path(output_dir) if output_dir else None
    if out_dir is None:
        from atlas.config import get_settings
        out_dir = Path(get_settings().data_dir) / "masters"
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    parts = [f"master_{kind}", stamp]
    if exposure_s is not None: parts.append(f"{exposure_s:g}s")
    if filter_name:            parts.append(filter_name)
    if ccd_temp_c is not None: parts.append(f"{ccd_temp_c:g}C")
    if gain is not None:       parts.append(f"g{gain}")
    out_path = out_dir / ("_".join(parts) + ".fits")

    # Write with original FITS header + stack metadata
    if ref_header is None:
        ref_header = fits.Header()
    ref_header["IMAGETYP"] = f"Master {kind.capitalize()}"
    ref_header["NCOMBINE"] = (len(cube_list), "Frames combined")
    ref_header["COMBTYPE"] = ("median", "Stacking method")
    ref_header["ATLASVER"] = ("1.0", "ATLAS stacking module")
    fits.PrimaryHDU(master, header=ref_header).writeto(out_path, overwrite=True)
    log.info("Wrote master %s (%.0f KB)", out_path, out_path.stat().st_size / 1024)

    # Register the master in calibration_masters
    from atlas.capture.ingest import register_calibration_master
    mid = register_calibration_master(out_path, kind=kind,
                                        n_frames=len(cube_list))
    return {
        "ok": True,
        "master_id": mid,
        "file_path": str(out_path),
        "n_frames": len(cube_list),
        "kind": kind,
        "params": {"filter": filter_name, "exposure_s": exposure_s,
                     "ccd_temp_c": ccd_temp_c, "gain": gain, "offset": offset},
    }
