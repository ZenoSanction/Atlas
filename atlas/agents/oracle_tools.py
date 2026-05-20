"""Tools the Oracle agent can use when chatted with."""
from __future__ import annotations

from datetime import datetime, timedelta

from atlas.agents.base import ToolSpec
from atlas.agents.state import get_state
from atlas.db.models import (
    Frame, KnowledgeThread, Measurement, Submission, Target,
)
from atlas.db.session import get_session


async def _last_research_pass(_p: dict) -> dict:
    info = get_state().get_oracle_last()
    if info is None:
        return {"last": None, "message": "No research pass yet."}
    return {"last": info}


async def _knowledge_summary(_p: dict) -> dict:
    with get_session() as s:
        rows = (s.query(KnowledgeThread).limit(50).all())
        for r in rows:
            s.expunge(r)
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r.state] = by_state.get(r.state, 0) + 1
    return {
        "thread_count": len(rows),
        "by_state": by_state,
        "threads": [
            {"id": r.id, "target_id": r.target_id, "kind": r.kind,
              "state": r.state,
              "open_question": r.open_question,
              "last_updated": r.last_updated.isoformat()}
            for r in rows[:20]
        ],
    }


async def _recent_data_volume(p: dict) -> dict:
    days = int(p.get("days", 7))
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as s:
        n_frames = s.query(Frame).filter(Frame.captured_at >= cutoff).count()
        n_meas = s.query(Measurement).filter(Measurement.epoch_utc >= cutoff).count()
        n_targets = s.query(Target).count()
    return {"days": days, "frames": n_frames,
            "measurements": n_meas, "targets_total": n_targets}


async def _pending_submissions(_p: dict) -> dict:
    with get_session() as s:
        rows = (s.query(Submission)
                  .filter(Submission.status == "queued")
                  .order_by(Submission.queued_at.desc())
                  .limit(50).all())
        for r in rows:
            s.expunge(r)
    return {
        "count": len(rows),
        "submissions": [
            {"id": r.id,
              "destination": r.destination.value if hasattr(r.destination, "value") else r.destination,
              "queued_at": r.queued_at.isoformat()}
            for r in rows
        ],
    }


ORACLE_TOOLS: list[ToolSpec] = [
    ToolSpec("get_last_research_pass",
             "Return the Oracle's most recent background-research summary "
             "(trigger, frame/measurement counts, timestamp).",
             {"type": "object", "properties": {}},
             _last_research_pass),
    ToolSpec("knowledge_thread_summary",
             "Summarise per-target knowledge threads — by state "
             "(dormant/active/mature/future), with a sample of the most "
             "recent threads.",
             {"type": "object", "properties": {}},
             _knowledge_summary),
    ToolSpec("recent_data_volume",
             "Count frames + measurements in the last N days, plus total "
             "targets known.",
             {"type": "object",
              "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}}},
             _recent_data_volume),
    ToolSpec("pending_submissions",
             "List submissions currently queued and waiting for human "
             "approval in the Science tab.",
             {"type": "object", "properties": {}},
             _pending_submissions),
]
