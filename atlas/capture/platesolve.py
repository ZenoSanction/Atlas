"""Plate solving via ASTAP — store the WCS solution on the frame row.

Given a registered frame id:
  1. Look up the file path from the DB.
  2. Run the configured ASTAP binary on it (via atlas.hardware.astap).
  3. Parse the resulting .ini sidecar into a WCS dict.
  4. Write back to frames.wcs_blob + frames.plate_solved = True.

ASTAP path comes from EquipmentProfile.astap_path. If it's None or the
binary doesn't exist, returns a structured error.
"""
from __future__ import annotations

from pathlib import Path

from atlas.db.session import get_session
from atlas.db.models import Frame
from atlas.logging_setup import get_logger

log = get_logger("capture.platesolve")


async def plate_solve_frame(frame_id: int, *,
                              radius_deg: float = 5.0,
                              timeout_s: float = 120.0) -> dict:
    """Solve a frame's FITS via ASTAP, persist the WCS, return result."""
    from atlas.db.managers import ConfigManager
    equip = ConfigManager.get_equipment()
    astap_path = (equip.astap_path if equip else None)
    if not astap_path:
        return {"error": "ASTAP path not configured (Setup → Equipment → ASTAP path)."}
    p = Path(astap_path)
    if not p.exists():
        return {"error": f"ASTAP binary not found at {astap_path}"}

    with get_session() as s:
        f = s.get(Frame, frame_id)
        if f is None:
            return {"error": f"Frame {frame_id} not found"}
        if not f.file_path or not Path(f.file_path).exists():
            return {"error": f"FITS file missing: {f.file_path}"}
        fits_path = f.file_path
        # Get pixel scale + FOV from equipment for ASTAP hints
        pixel_scale = (equip.pixel_scale_arcsec if equip else None)

    from atlas.hardware.astap import AstapClient, AstapError
    client = AstapClient(astap_path=astap_path)
    fov_deg = None
    if pixel_scale:
        # Rough FOV hint: assume ~3000 px wide sensor; refined per real
        # camera in a later pass. ASTAP uses this as a search hint only.
        fov_deg = (3000 * float(pixel_scale)) / 3600.0
    try:
        wcs = await client.solve(fits_path, fov_deg=fov_deg,
                                   radius_deg=radius_deg,
                                   timeout_s=timeout_s)
    except AstapError as e:
        return {"error": f"ASTAP: {e}", "frame_id": frame_id}
    except Exception as e:
        return {"error": f"Plate-solve failed: {type(e).__name__}: {e}"}

    # Persist
    with get_session() as s:
        f = s.get(Frame, frame_id)
        if f is None:
            return {"error": "frame disappeared"}
        f.plate_solved = True
        f.wcs_blob = {k: v for k, v in wcs.items() if k != "wcs_text"}
        # Capture central RA/Dec into the frame's wcs_blob for quick lookups
        ra_c = wcs.get("CRVAL1") or wcs.get("RA")
        dec_c = wcs.get("CRVAL2") or wcs.get("DEC")
    log.info("Solved frame #%d (centre RA=%.4f Dec=%.4f)",
              frame_id,
              float(ra_c) if ra_c else 0.0,
              float(dec_c) if dec_c else 0.0)
    return {
        "ok": True,
        "frame_id": frame_id,
        "fits_path": fits_path,
        "wcs_keys_set": list(wcs.keys()),
        "centre_ra_deg": (float(ra_c) if ra_c else None),
        "centre_dec_deg": (float(dec_c) if dec_c else None),
    }


async def plate_solve_recent_lights(limit: int = 10) -> dict:
    """Walk the most recent unplated light frames and solve each."""
    with get_session() as s:
        rows = (s.query(Frame.id)
                  .filter(Frame.frame_type == "light",
                            Frame.plate_solved.is_(False))
                  .order_by(Frame.id.desc())
                  .limit(limit).all())
        ids = [r[0] for r in rows]
    results = []
    for fid in ids:
        results.append(await plate_solve_frame(fid))
    success = sum(1 for r in results if r.get("ok"))
    return {"attempted": len(results), "solved": success,
            "details": results}
