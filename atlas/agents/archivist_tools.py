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
]
