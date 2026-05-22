"""Recovery helpers run at boot to clean up state left in flight by an
unclean shutdown (power loss, OS update, kill -9, etc.).

These are intentionally conservative — they close out things that
can't possibly still be alive in the new process, but never delete
data. The audit trail tells the operator exactly what was recovered.
"""
from __future__ import annotations

from datetime import datetime

from atlas.db.models import Session, SessionState
from atlas.db.session import get_session
from atlas.logging_setup import get_logger

log = get_logger("db.recovery")


def recover_orphan_sessions() -> int:
    """Close any Session rows still in NOMINAL / WARNING that lack an
    ended_at. Returns the count of rows closed.

    Why: when ATLAS is killed mid-session, the in-memory session id
    is lost and the Session row sits at NOMINAL forever. Subsequent
    boots can't tell the old session apart from a real running one,
    and the verdict / planning logic may refuse to start a new one
    because "a session is already active." Closing the orphan rows
    once at boot resolves that cleanly.

    Note we mark COMPLETE, not FAILED, because the data captured
    before the crash is still valid science. The reason field
    records the recovery so the morning report explains the gap.
    """
    now = datetime.utcnow()
    closed = 0
    with get_session() as s:
        orphans = (s.query(Session)
                     .filter(Session.ended_at.is_(None))
                     .filter(Session.state.in_([SessionState.PRE_SESSION,
                                                  SessionState.NOMINAL,
                                                  SessionState.WARNING,
                                                  SessionState.CRITICAL,
                                                  SessionState.STANDBY_LIGHT,
                                                  SessionState.STANDBY_FULL]))
                     .all())
        for sess in orphans:
            prior_state = sess.state.value if hasattr(sess.state, "value") else str(sess.state)
            sess.ended_at = now
            sess.state = SessionState.COMPLETE
            sess.state_reason = (
                f"Recovered from unclean shutdown at boot "
                f"{now.isoformat(timespec='seconds')}Z "
                f"(was {prior_state})"
            )
            existing = sess.final_summary or ""
            sess.final_summary = (
                existing
                + ("\n" if existing else "")
                + f"[recovery {now.isoformat(timespec='seconds')}Z] "
                  f"Closed at boot — prior process did not record an "
                  f"ended_at. State at recovery: {prior_state}."
            ).strip()
            closed += 1
            log.warning(
                "Recovered orphan Session #%d (was state=%s, started %s)",
                sess.id, prior_state,
                sess.started_at.isoformat(timespec='seconds')
                  if sess.started_at else "?",
            )
    return closed
