"""Submission lifecycle engine.

Bridges the Submission table (QUEUED → APPROVED → SUBMITTED → ACK) with
the per-destination formatter+sender classes.

Flow:
  1. Archivist produces a Measurement row (Phase 2 science pipelines).
  2. ATLAS calls prepare_submission(measurement_id, destination):
       — looks up the right Submitter
       — calls format(measurement_row) → fills Submission.formatted_payload
       — sets status=QUEUED
  3. Operator (via dashboard Science tab) approves → status=APPROVED.
  4. Periodic worker (or operator-invoked tool) calls send_approved():
       — picks up APPROVED rows
       — runs sender.send(payload)
       — on success: status=SUBMITTED + response_payload set
       — on fail: status=FAILED + rejected_reason set

The engine is conservative: never sends without an APPROVED status.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from atlas.db.session import get_session
from atlas.db.models import (
    Submission, SubmissionStatus, SubmissionDestination, Measurement, Target,
)
from atlas.logging_setup import get_logger

log = get_logger("science.submission_engine")


def _submitter_for(destination: str):
    """Return the right Submitter instance for a destination string."""
    dest = destination.lower() if isinstance(destination, str) else destination
    if dest == "mpc":
        from atlas.science.submissions.mpc import MpcSubmitter
        return MpcSubmitter()
    if dest == "aavso":
        from atlas.science.submissions.aavso import AavsoSubmitter
        return AavsoSubmitter()
    if dest == "tns":
        from atlas.science.submissions.tns import TnsSubmitter
        return TnsSubmitter()
    if dest in ("nasa_exoplanet_watch", "nasa_eo"):
        from atlas.science.submissions.nasa_eo import NasaEoSubmitter
        return NasaEoSubmitter()
    raise ValueError(f"No submitter registered for destination {destination!r}")


def _measurement_to_row(m: Measurement) -> dict:
    """Build the dict the formatters expect. Pulls target_name when set."""
    target_name = None
    with get_session() as s:
        if m.target_id:
            t = s.get(Target, m.target_id)
            target_name = t.name if t else None
    val = dict(m.value or {})
    val.setdefault("target_name", target_name)
    return {
        "id": m.id,
        "epoch_utc": m.epoch_utc,
        "target_id": m.target_id,
        "target_name": target_name,
        "session_id": m.session_id,
        "frame_id": m.frame_id,
        "workflow": m.workflow.value if hasattr(m.workflow, "value") else str(m.workflow),
        "kind": m.kind.value if hasattr(m.kind, "value") else str(m.kind),
        "value": val,
        "quality": m.quality,
        "notes": m.notes,
    }


def prepare_submission(measurement_id: int, destination: str) -> dict:
    """Format a measurement for a destination and queue a Submission row.
    Returns submission_id + payload preview, or error dict."""
    with get_session() as s:
        m = s.get(Measurement, measurement_id)
        if m is None:
            return {"error": f"Measurement {measurement_id} not found"}
        s.expunge(m)
    try:
        submitter = _submitter_for(destination)
    except ValueError as e:
        return {"error": str(e)}
    row = _measurement_to_row(m)
    try:
        payload = submitter.format(row)
    except Exception as e:
        return {"error": f"Format failed: {type(e).__name__}: {e}",
                "measurement_id": measurement_id, "destination": destination}
    # Queue the submission
    with get_session() as s:
        sub = Submission(
            measurement_id=measurement_id,
            destination=SubmissionDestination(destination)
                          if isinstance(destination, str) else destination,
            status=SubmissionStatus.QUEUED,
            formatted_payload=payload.text,
        )
        s.add(sub); s.flush()
        sid = sub.id
    log.info("Submission %d queued for %s (measurement %d)",
              sid, destination, measurement_id)
    return {"ok": True, "submission_id": sid,
            "destination": destination,
            "measurement_id": measurement_id,
            "preview": payload.text[:400],
            "metadata": payload.metadata or {}}


async def send_one_approved(submission_id: int) -> dict:
    """Run the sender for one APPROVED submission. Updates row + returns."""
    with get_session() as s:
        sub = s.get(Submission, submission_id)
        if sub is None:
            return {"error": f"Submission {submission_id} not found"}
        if sub.status != SubmissionStatus.APPROVED:
            return {"error": (f"Submission {submission_id} is "
                                f"{sub.status.value if hasattr(sub.status, 'value') else sub.status}, "
                                "not APPROVED. Only approved rows are sent.")}
        s.expunge(sub)
    dest = sub.destination.value if hasattr(sub.destination, "value") else sub.destination
    try:
        submitter = _submitter_for(dest)
    except ValueError as e:
        return {"error": str(e)}
    from atlas.science.submissions.base import SubmissionPayload
    payload = SubmissionPayload(text=sub.formatted_payload or "",
                                  content_type="text/plain")
    try:
        result = await submitter.send(payload)
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    with get_session() as s:
        sub2 = s.get(Submission, submission_id)
        if sub2 is None:
            return {"error": "submission disappeared mid-send"}
        if result.get("ok"):
            sub2.status = SubmissionStatus.SUBMITTED
            sub2.submitted_at = datetime.utcnow()
            sub2.response_payload = str(result.get("response", result))[:4000]
        else:
            sub2.status = SubmissionStatus.FAILED
            sub2.rejected_at = datetime.utcnow()
            sub2.rejected_reason = (result.get("error") or "send failed")[:500]
    log.info("Submission %d → %s: %s",
              submission_id, dest,
              "SUBMITTED" if result.get("ok") else "FAILED")
    return {"submission_id": submission_id, "destination": dest,
            "status": "SUBMITTED" if result.get("ok") else "FAILED",
            "result": result}


async def send_all_approved(limit: int = 25) -> dict:
    """Sweep all APPROVED submissions and try to send each. Returns
    counts + per-submission results."""
    with get_session() as s:
        rows = (s.query(Submission.id)
                  .filter(Submission.status == SubmissionStatus.APPROVED)
                  .order_by(Submission.approved_at.asc())
                  .limit(limit).all())
        ids = [r[0] for r in rows]
    results = []
    for sid in ids:
        results.append(await send_one_approved(sid))
    submitted = sum(1 for r in results if r.get("status") == "SUBMITTED")
    failed = sum(1 for r in results if r.get("status") == "FAILED")
    return {"attempted": len(results), "submitted": submitted,
            "failed": failed, "results": results}
