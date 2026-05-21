"""Tools the Archivist agent can use when chatted with."""
from __future__ import annotations

from datetime import datetime, timedelta

from atlas.agents.base import ToolSpec
from atlas.agents.state import get_state
from atlas.db.models import Frame, Measurement, Session as SessionRow, CalibrationMaster
from atlas.db.session import get_session


async def _last_archive(_p: dict) -> dict:
    info = get_state().get_archivist_last()
    if info is None:
        return {"last": None, "message": "No archive activity yet."}
    return {"last": info}


async def _count_recent_frames(p: dict) -> dict:
    days = int(p.get("days", 7))
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as s:
        n = s.query(Frame).filter(Frame.captured_at >= cutoff).count()
    return {"days": days, "frame_count": n}


async def _count_measurements(p: dict) -> dict:
    days = int(p.get("days", 7))
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as s:
        n = s.query(Measurement).filter(Measurement.epoch_utc >= cutoff).count()
    return {"days": days, "measurement_count": n}


async def _list_recent_sessions(p: dict) -> dict:
    limit = int(p.get("limit", 10))
    with get_session() as s:
        rows = (s.query(SessionRow)
                  .order_by(SessionRow.started_at.desc())
                  .limit(limit).all())
        for r in rows:
            s.expunge(r)
    return {
        "count": len(rows),
        "sessions": [
            {"id": r.id,
              "state": r.state.value if hasattr(r.state, "value") else str(r.state),
              "started_at": r.started_at.isoformat(),
              "ended_at": r.ended_at.isoformat() if r.ended_at else None,
              "simulation": bool(r.simulation)}
            for r in rows
        ],
    }


async def _calibration_freshness(_p: dict) -> dict:
    with get_session() as s:
        rows = (s.query(CalibrationMaster)
                  .order_by(CalibrationMaster.created_at.desc())
                  .limit(5).all())
        for r in rows:
            s.expunge(r)
    return {
        "count": len(rows),
        "masters": [
            {"id": r.id, "kind": r.kind, "filter": r.filter_name,
              "exposure_s": r.exposure_s, "ccd_temp_c": r.ccd_temp_c,
              "n_frames": r.n_frames,
              "created_at": r.created_at.isoformat()}
            for r in rows
        ],
    }


async def _register_frame(p: dict) -> dict:
    file_path = (p.get("file_path") or "").strip()
    if not file_path:
        return {"error": "file_path is required (absolute path to a FITS file)."}
    from atlas.capture.ingest import register_frame as _reg
    try:
        fid = _reg(file_path, frame_type=p.get("frame_type"))
    except FileNotFoundError:
        return {"error": f"File not found: {file_path}"}
    except Exception as e:
        return {"error": f"Failed: {type(e).__name__}: {e}"}
    return {"ok": True, "frame_id": fid, "file_path": file_path,
            "message": f"Registered frame #{fid}."}


async def _register_calibration_master(p: dict) -> dict:
    file_path = (p.get("file_path") or "").strip()
    kind = (p.get("kind") or "").lower().strip()
    if not file_path:
        return {"error": "file_path is required."}
    if kind not in ("bias", "dark", "flat"):
        return {"error": "kind must be one of: bias, dark, flat."}
    from atlas.capture.ingest import register_calibration_master as _reg
    try:
        mid = _reg(file_path, kind, n_frames=p.get("n_frames"))
    except FileNotFoundError:
        return {"error": f"File not found: {file_path}"}
    except Exception as e:
        return {"error": f"Failed: {type(e).__name__}: {e}"}
    return {"ok": True, "master_id": mid, "kind": kind, "file_path": file_path,
            "message": f"Registered {kind} master #{mid}. The pre-flight "
                          "Calibration Library gate will pick this up on the "
                          "next 2-minute cycle."}


async def _plate_solve_recent(p: dict) -> dict:
    limit = int(p.get("limit", 10))
    from atlas.capture.platesolve import plate_solve_recent_lights
    return await plate_solve_recent_lights(limit=limit)


async def _plate_solve_one(p: dict) -> dict:
    frame_id = p.get("frame_id")
    if frame_id is None:
        return {"error": "frame_id is required"}
    from atlas.capture.platesolve import plate_solve_frame
    return await plate_solve_frame(int(frame_id))


async def _grade_recent(p: dict) -> dict:
    limit = int(p.get("limit", 50))
    from atlas.capture.quality import grade_ungraded
    results = grade_ungraded(limit=limit)
    counts = {"A": 0, "B": 0, "C": 0, "D": 0, "UNGRADED": 0, "error": 0}
    for r in results:
        if r.get("error"):
            counts["error"] += 1
        else:
            counts[r.get("grade", "UNGRADED")] += 1
    return {"ok": True, "graded": len(results), "by_grade": counts}


async def _stack_master(p: dict) -> dict:
    kind = (p.get("kind") or "").lower().strip()
    if kind not in ("bias", "dark", "flat"):
        return {"error": "kind must be bias / dark / flat"}
    from atlas.capture.stack import stack_master as _do
    try:
        return _do(
            kind=kind,
            filter_name=p.get("filter_name"),
            exposure_s=(float(p["exposure_s"]) if p.get("exposure_s") is not None else None),
            ccd_temp_c=(float(p["ccd_temp_c"]) if p.get("ccd_temp_c") is not None else None),
            gain=(int(p["gain"]) if p.get("gain") is not None else None),
            offset=(int(p["offset"]) if p.get("offset") is not None else None),
            min_frames=int(p.get("min_frames", 5)),
        )
    except Exception as e:
        return {"error": f"Stacking failed: {type(e).__name__}: {e}"}


async def _ingest_directory(p: dict) -> dict:
    directory = (p.get("directory") or "").strip()
    if not directory:
        return {"error": "directory is required (path to scan)."}
    recursive = bool(p.get("recursive", True))
    from atlas.capture.ingest import ingest_directory as _ing
    try:
        ids = _ing(directory, recursive=recursive)
    except NotADirectoryError:
        return {"error": f"Not a directory: {directory}"}
    except Exception as e:
        return {"error": f"Failed: {type(e).__name__}: {e}"}
    return {"ok": True, "count": len(ids), "frame_ids": ids[:50],
            "truncated": len(ids) > 50,
            "message": f"Ingested {len(ids)} new frame(s) from {directory}. "
                          "Use count_recent_frames to verify totals."}


ARCHIVIST_TOOLS: list[ToolSpec] = [
    ToolSpec("get_last_archive_activity",
             "Return the Archivist's most recent post-session activity "
             "(report path, counts, alerts).",
             {"type": "object", "properties": {}},
             _last_archive),
    ToolSpec("count_recent_frames",
             "Count light/dark/bias/flat frames captured in the last N days.",
             {"type": "object",
              "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}}},
             _count_recent_frames),
    ToolSpec("count_recent_measurements",
             "Count scientific measurements (astrometry/photometry/transient) "
             "produced in the last N days.",
             {"type": "object",
              "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}}},
             _count_measurements),
    ToolSpec("list_recent_sessions",
             "List the most recent observing sessions with state and duration.",
             {"type": "object",
              "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}},
             _list_recent_sessions),
    ToolSpec("calibration_freshness",
             "Return the 5 most recent calibration masters (bias/dark/flat) "
             "with their acquisition parameters so the user can see "
             "whether the library is fresh.",
             {"type": "object", "properties": {}},
             _calibration_freshness),
    ToolSpec("register_frame",
             "Register a single FITS file in the frames table. Reads "
             "metadata from the FITS header (DATE-OBS, EXPTIME, FILTER, "
             "CCD-TEMP, GAIN, OFFSET, IMAGETYP) so the operator only "
             "needs to give the path. Use frame_type to override the "
             "header's IMAGETYP if needed (light / dark / bias / flat).",
             {"type": "object",
              "properties": {
                  "file_path": {"type": "string",
                                  "description": "Absolute path to a *.fit / *.fits file."},
                  "frame_type": {"type": "string",
                                   "enum": ["light", "dark", "bias", "flat"],
                                   "description": "Optional override for the FITS IMAGETYP."},
              },
              "required": ["file_path"]},
             _register_frame),
    ToolSpec("register_calibration_master",
             "Register an existing master frame (bias / dark / flat) in "
             "the calibration_masters table. The pre-flight Calibration "
             "Library gate uses this table; registering a fresh master "
             "flips the gate from yellow ('library empty') to green. "
             "Use this for masters you've already built in NINA / PixInsight "
             "/ Siril and want ATLAS to know about.",
             {"type": "object",
              "properties": {
                  "file_path": {"type": "string",
                                  "description": "Absolute path to the master FITS file."},
                  "kind": {"type": "string", "enum": ["bias", "dark", "flat"]},
                  "n_frames": {"type": "integer",
                                 "description": "How many sub-frames went into the master."},
              },
              "required": ["file_path", "kind"]},
             _register_calibration_master),
    ToolSpec("ingest_directory",
             "Scan a directory for FITS files and register every one it "
             "finds in the frames table. Already-known files (same path) "
             "are skipped. Use for bulk-importing an existing capture "
             "library — e.g. point at NINA's image-save folder.",
             {"type": "object",
              "properties": {
                  "directory": {"type": "string",
                                  "description": "Absolute path to scan."},
                  "recursive": {"type": "boolean",
                                  "description": "Descend into subdirectories (default true)."},
              },
              "required": ["directory"]},
             _ingest_directory),
    ToolSpec("plate_solve_frame",
             "Plate-solve one frame via ASTAP. Writes the WCS solution "
             "to frames.wcs_blob and sets plate_solved=True. ASTAP binary "
             "must be installed and the path set in Setup → Equipment.",
             {"type": "object",
              "properties": {"frame_id": {"type": "integer"}},
              "required": ["frame_id"]},
             _plate_solve_one),
    ToolSpec("plate_solve_recent_lights",
             "Plate-solve up to N recent unplated light frames in one go. "
             "Skips bias/dark/flat. Useful after a capture session: each "
             "successfully-solved frame's centre RA/Dec is recorded for "
             "downstream photometry/astrometry workflows.",
             {"type": "object",
              "properties": {
                  "limit": {"type": "integer", "minimum": 1, "maximum": 100},
              }},
             _plate_solve_recent),
    ToolSpec("grade_recent_frames",
             "Grade up to N ungraded frames (FWHM-based or star-count-"
             "based heuristic), writing A/B/C/D back to frames.quality. "
             "Calibration frames (bias/dark/flat) auto-grade A. The "
             "watch-folder ingest also auto-grades, so this tool is mostly "
             "useful for re-grading after a heuristic change.",
             {"type": "object",
              "properties": {
                  "limit": {"type": "integer", "minimum": 1, "maximum": 500},
              }},
             _grade_recent),
    ToolSpec("stack_master",
             "Median-stack matching frames into a master bias/dark/flat, "
             "write the master FITS to data/masters/, and auto-register "
             "it in the calibration library. Frames must already be in "
             "the frames table (use ingest_directory or register_frame "
             "first). Match by kind + optional filter/exposure/temp/gain "
             "/offset. Returns master_id + file_path + count combined.",
             {"type": "object",
              "properties": {
                  "kind": {"type": "string", "enum": ["bias", "dark", "flat"]},
                  "filter_name": {"type": "string",
                                    "description": "Required for flats; optional for bias/dark."},
                  "exposure_s": {"type": "number",
                                   "description": "Match exposure ±0.05 s. Required for dark/flat."},
                  "ccd_temp_c": {"type": "number",
                                   "description": "Match CCD temp ±0.5 °C."},
                  "gain": {"type": "integer"},
                  "offset": {"type": "integer"},
                  "min_frames": {"type": "integer", "minimum": 1,
                                   "description": "Refuse to stack fewer than this many. Default 5."},
              },
              "required": ["kind"]},
             _stack_master),
]
