"""FITS-file ingestion into ATLAS's frames + calibration_masters tables.

The ATLAS pipeline assumes ``frames`` and ``calibration_masters`` are
populated. Phase 1 left this up to "Phase 2 ingestion"; this module is
that ingestion.

Usage:
    register_frame("C:/path/to/light_M42_300s.fits")
    register_calibration_master("C:/path/to/master_dark_300s_-10C.fits", kind="dark")
    ingest_directory("C:/captures/2026-05-20/")

All three are exposed as Archivist chat tools so the operator can
populate the library without leaving the dashboard.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from atlas.db.session import get_session
from atlas.db.models import Frame, FrameQuality, CalibrationMaster
from atlas.logging_setup import get_logger

log = get_logger("capture.ingest")


# ---- Helpers ---------------------------------------------------------------

_TYPE_ALIASES = {
    "light": "light", "light frame": "light", "object": "light",
    "dark": "dark", "dark frame": "dark",
    "bias": "bias", "bias frame": "bias", "zero": "bias",
    "flat": "flat", "flat field": "flat", "flat frame": "flat",
}

# FITS keys we look at, in order of preference. NINA, ASIAir, SharpCap and
# most other capture apps follow these conventions; we fall back gracefully
# when keys are missing.
_HEADER_MAP = {
    "exposure_s":   ("EXPTIME", "EXPOSURE"),
    "filter":       ("FILTER",),
    "ccd_temp_c":   ("CCD-TEMP", "CCDTEMP"),
    "gain":         ("GAIN", "EGAIN"),
    "offset":       ("OFFSET",),
    "object":       ("OBJECT",),
    "frame_type":   ("IMAGETYP", "FRAMETYP"),
    "ra_deg":       ("RA", "CRVAL1", "OBJCTRA"),
    "dec_deg":      ("DEC", "CRVAL2", "OBJCTDEC"),
    "fwhm_arcsec":  ("FWHM", "STARFWHM"),
    "date_obs":     ("DATE-OBS", "DATE_OBS"),
}


def _first(d: dict, keys: tuple) -> Any:
    for k in keys:
        if k in d:
            v = d[k]
            if v not in (None, "", "N/A"):
                return v
    return None


def _coerce_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_int(v: Any) -> int | None:
    f = _coerce_float(v)
    return int(f) if f is not None else None


def _parse_date(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                 "%Y-%m-%d %H:%M:%S", "%Y/%m/%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ---- FITS header reader ----------------------------------------------------

def read_fits_header(path: Path | str) -> dict:
    """Pull the metadata ATLAS cares about out of a FITS primary header.

    Returns a flat dict with our internal field names + a ``fits_header``
    key holding the full header as a string→string mapping for the
    archival ``frames.fits_header`` JSON column. Returns ``{}`` if the
    file isn't readable as FITS."""
    p = Path(path)
    try:
        from astropy.io import fits
    except ImportError:
        log.error("astropy not installed — can't read FITS")
        return {}
    if not p.exists():
        raise FileNotFoundError(str(p))
    out: dict[str, Any] = {}
    try:
        with fits.open(p, memmap=False, ignore_missing_simple=True) as hdul:
            h = hdul[0].header
    except Exception as e:
        log.warning("Couldn't open %s as FITS: %s", p, e)
        return {}

    for field, keys in _HEADER_MAP.items():
        out[field] = _first(h, keys)
    out["fits_header"] = {str(k): str(v) for k, v in h.items() if k}
    return out


# ---- Frame registration ----------------------------------------------------

def register_frame(path: Path | str,
                    *,
                    frame_type: str | None = None,
                    session_id: int | None = None,
                    target_id: int | None = None,
                    **overrides) -> int:
    """Register a single FITS file in the ``frames`` table.

    Header values are read via ``read_fits_header``; explicit kwargs
    override anything from the header. Returns the new row id."""
    p = Path(path).resolve()
    meta = read_fits_header(p)
    meta.update({k: v for k, v in overrides.items() if v is not None})

    raw_type = (frame_type
                 or (str(meta.get("frame_type") or "").lower().strip()
                       or "light"))
    fr_type = _TYPE_ALIASES.get(raw_type, "light")
    captured_at = _parse_date(meta.get("date_obs")) or datetime.utcnow()

    with get_session() as s:
        f = Frame(
            session_id=session_id,
            target_id=target_id,
            captured_at=captured_at,
            file_path=str(p),
            frame_type=fr_type,
            filter_name=meta.get("filter"),
            exposure_s=_coerce_float(meta.get("exposure_s")) or 0.0,
            gain=_coerce_int(meta.get("gain")),
            offset=_coerce_int(meta.get("offset")),
            ccd_temp_c=_coerce_float(meta.get("ccd_temp_c")),
            fwhm_arcsec=_coerce_float(meta.get("fwhm_arcsec")),
            quality=FrameQuality.UNGRADED,
            plate_solved=False,
            fits_header=meta.get("fits_header"),
        )
        s.add(f)
        s.flush()
        log.info("Registered frame #%d (%s) %s", f.id, fr_type, p.name)
        return f.id


# ---- Calibration master registration --------------------------------------

def register_calibration_master(path: Path | str,
                                  kind: str,
                                  *,
                                  n_frames: int | None = None,
                                  **overrides) -> int:
    """Register an existing master frame (bias/dark/flat) in
    ``calibration_masters``. ``kind`` must be one of: bias, dark, flat.

    Header values populate filter/exposure/temp/gain/offset; explicit
    kwargs override. Returns the new row id."""
    p = Path(path).resolve()
    if kind not in ("bias", "dark", "flat"):
        raise ValueError(f"kind must be bias/dark/flat, got {kind!r}")
    meta = read_fits_header(p)
    meta.update({k: v for k, v in overrides.items() if v is not None})

    with get_session() as s:
        m = CalibrationMaster(
            kind=kind,
            filter_name=(meta.get("filter") if kind == "flat" else meta.get("filter")),
            exposure_s=_coerce_float(meta.get("exposure_s")),
            ccd_temp_c=_coerce_float(meta.get("ccd_temp_c")),
            gain=_coerce_int(meta.get("gain")),
            offset=_coerce_int(meta.get("offset")),
            file_path=str(p),
            n_frames=int(n_frames or 1),
        )
        s.add(m)
        s.flush()
        log.info("Registered %s master #%d: %s", kind, m.id, p.name)
        return m.id


# ---- Bulk directory ingest ------------------------------------------------

def ingest_directory(directory: Path | str,
                       *,
                       recursive: bool = True,
                       glob: str = "*.fit*",
                       session_id: int | None = None) -> list[int]:
    """Walk a directory for FITS files and register each as a frame.

    Skips files already registered (matching file_path). Returns list of
    new frame ids inserted this call."""
    d = Path(directory)
    if not d.exists() or not d.is_dir():
        raise NotADirectoryError(str(d))
    walker = d.rglob(glob) if recursive else d.glob(glob)
    ids: list[int] = []
    skipped = 0
    with get_session() as s:
        existing_paths = {row[0] for row in s.query(Frame.file_path).all()}
    for f in walker:
        if str(f.resolve()) in existing_paths:
            skipped += 1
            continue
        try:
            ids.append(register_frame(f, session_id=session_id))
        except Exception as e:
            log.warning("Skipping %s: %s", f, e)
    log.info("ingest_directory %s: %d new, %d already known",
              d, len(ids), skipped)
    return ids
