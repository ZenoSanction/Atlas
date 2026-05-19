"""Archivist agent — post-session processing, reports, memory.

Phase 1 behaviour (this file):
  - On POST_SESSION trigger, count the session's frames/measurements/alerts,
    write a plain-text session report stub to data/reports/, store the most
    recent activity in shared state, broadcast a `session_archived` event,
    and notify the Oracle that new data is available.

Phase 2 TODOs (clearly marked below):
  - Real calibration pipeline: pair every light frame with its matching
    master bias/dark/flat by (filter, exposure, temp, gain).
  - Stack per workflow: Siril script for deep-sky, AutoStakkert!4 for
    planetary, custom for transient/photometry.
  - Plate-solve every science frame with ASTAP, embed WCS in FITS header.
  - Measurement extraction (astrometric centroids, PSF photometry, etc.).
  - Queue eligible measurements as Submissions(status=QUEUED).
  - Render the 10-section HTML session report described in the brochure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from atlas.agents.base import BaseAgent
from atlas.agents.state import get_state
from atlas.config import get_settings
from atlas.db.managers import AlertManager, SessionManager
from atlas.db.models import AgentMessageKind, AgentName
from atlas.db.session import get_session
from atlas.db.models import Frame, Measurement, Session as SessionRow


# Lightweight idle pulse so the Agent Activity feed has the Archivist
# checking in periodically rather than going totally silent between
# sessions. 10 minutes is plenty for a post-session-only agent.
IDLE_HEARTBEAT_S = 600


class Archivist(BaseAgent):
    name = AgentName.ARCHIVIST

    def __init__(self) -> None:
        super().__init__()
        self._last_heartbeat = 0.0

    async def run(self) -> None:
        self.log.info("Archivist agent online — awaits POST_SESSION triggers")
        while not self.should_stop:
            msg = await self.recv_with_timeout(timeout_s=30.0)
            if msg is None:
                # Idle pulse, infrequent
                now = asyncio.get_event_loop().time()
                if now - self._last_heartbeat >= IDLE_HEARTBEAT_S:
                    await self._idle_heartbeat()
                    self._last_heartbeat = now
                continue
            if msg.kind == AgentMessageKind.POST_SESSION:
                await self._process_session(msg)
            else:
                self.log.debug("Archivist ignoring kind: %s", msg.kind)

    async def _idle_heartbeat(self) -> None:
        """Emit a benign 'still here' tick so the dashboard sees the agent."""
        info = {
            "type": "idle",
            "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        get_state().set_archivist_last(info)
        await self.bus.broadcast_event({
            "type": "archivist_tick",
            "sender": "archivist",
            "kind": "idle_heartbeat",
            "summary": "Archivist standing by; no session to process.",
            "sent_at": info["at"],
        })

    async def _process_session(self, msg) -> None:
        session_id = msg.session_id
        self.log.info("Processing session %s", session_id)

        # Count what's in this session — read-only, no pipeline yet
        with get_session() as s:
            sess: SessionRow | None = s.get(SessionRow, session_id) if session_id else None
            n_frames = (s.query(Frame).filter(Frame.session_id == session_id).count()
                         if session_id else 0)
            n_measurements = (s.query(Measurement).filter(Measurement.session_id == session_id).count()
                               if session_id else 0)
            sess_started = sess.started_at if sess else None
            sess_ended = sess.ended_at if sess else None

        alerts = AlertManager.unresolved(session_id=session_id) if session_id else []

        # Write the report stub
        settings = get_settings()
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        report_path = settings.reports_dir / f"session_{session_id or 'none'}_{stamp}.txt"
        report_path.write_text(
            "\n".join([
                f"ATLAS session report — STUB",
                f"Session ID:    {session_id}",
                f"Started UTC:   {sess_started}",
                f"Ended UTC:     {sess_ended}",
                f"Frames:        {n_frames}",
                f"Measurements:  {n_measurements}",
                f"Open alerts:   {len(alerts)}",
                "",
                "# TODO Phase 2: full 10-section report with calibration sources,",
                "# per-target stacks, measurement audit, decision audit, etc.",
            ]),
            encoding="utf-8",
        )

        info = {
            "session_id": session_id,
            "report_path": str(report_path),
            "n_frames": n_frames,
            "n_measurements": n_measurements,
            "n_open_alerts": len(alerts),
            "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        get_state().set_archivist_last(info)

        self.log_decision("session_processed",
                           inputs={"session_id": session_id},
                           outputs={"report_path": str(report_path),
                                     "n_frames": n_frames,
                                     "n_measurements": n_measurements},
                           rationale="Phase-1 stub: counted artifacts and wrote report skeleton",
                           session_id=session_id)

        await self.bus.broadcast_event({
            "type": "session_archived",
            "sender": "archivist",
            "kind": "session_archived",
            "session_id": session_id,
            "n_frames": n_frames,
            "n_measurements": n_measurements,
            "report_path": str(report_path),
            "summary": (f"Session {session_id}: {n_frames} frames, "
                          f"{n_measurements} measurements, "
                          f"{len(alerts)} open alerts."),
            "sent_at": info["at"],
        })

        # Notify Oracle as before so its research pass can pick up new data
        await self.send(
            AgentName.ORACLE, AgentMessageKind.NEW_DATA,
            payload={"session_id": session_id,
                       "n_frames": n_frames,
                       "n_measurements": n_measurements},
            session_id=session_id,
        )

    async def safe_mode_step(self) -> None:
        await asyncio.sleep(60)
