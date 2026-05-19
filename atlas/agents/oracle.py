"""Oracle agent — research, anomaly detection, transient pipeline.

Phase 1 behaviour (this file):
  - On NEW_DATA from the Archivist: log the receipt, count the recent
    frames/measurements in the database, broadcast a `research_pass` event,
    and store the most recent activity in shared state.
  - Periodic idle pass every 30 minutes: count recent frames and broadcast
    a benign 'research scan' tick so the Agent Activity feed shows the
    Oracle is alive.

Phase 2 TODOs (clearly marked below):
  - Image subtraction on transient-flagged frames (HOTPANTS / PyZOGY).
  - Cross-match candidates against Gaia DR3, Pan-STARRS, MPC, recent TNS.
  - Queue confirmed transient candidates as Submissions(TNS, QUEUED).
  - Photometric baseline analysis per knowledge thread (variable star,
    exoplanet) — light-curve assembly with proper error propagation.
  - Knowledge-thread state transitions (dormant -> active -> mature).
  - Anomaly classification across unrelated targets in the same session
    (instrument-vs-physics discriminator).
  - Research agenda intake (AAVSO, ATel, MPC NEO confirmation page,
    NASA Exoplanet Watch transit predictions).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from atlas.agents.base import BaseAgent
from atlas.agents.state import get_state
from atlas.db.models import AgentMessageKind, AgentName
from atlas.db.session import get_session
from atlas.db.models import Frame, Measurement


IDLE_PASS_INTERVAL_S = 30 * 60   # 30 minutes


class Oracle(BaseAgent):
    name = AgentName.ORACLE

    def __init__(self) -> None:
        super().__init__()
        self._last_idle = 0.0
        self._initial_done = False

    async def run(self) -> None:
        self.log.info("Oracle agent online — research + transient pipeline")
        while not self.should_stop:
            if not self._initial_done:
                self._initial_done = True
                try:
                    await self._idle_research(reason="startup")
                except Exception:
                    self.log.exception("Initial idle pass failed")
                self._last_idle = asyncio.get_event_loop().time()

            msg = await self.recv_with_timeout(timeout_s=60.0)
            if msg is None:
                # Periodic background pass
                now = asyncio.get_event_loop().time()
                if now - self._last_idle >= IDLE_PASS_INTERVAL_S:
                    try:
                        await self._idle_research(reason="periodic")
                    except Exception:
                        self.log.exception("Idle research pass failed")
                    self._last_idle = now
                continue
            if msg.kind == AgentMessageKind.NEW_DATA:
                await self._handle_new_data(msg)
            else:
                self.log.debug("Oracle ignoring kind: %s", msg.kind)

    async def _handle_new_data(self, msg) -> None:
        session_id = msg.payload.get("session_id")
        n_frames = msg.payload.get("n_frames")
        n_measurements = msg.payload.get("n_measurements")
        self.log.info("Oracle: new data — session=%s frames=%s measurements=%s",
                        session_id, n_frames, n_measurements)

        # TODO Phase 2: real pipeline (see module docstring). For now we just
        # acknowledge in the audit trail and broadcast so the user can see
        # the Oracle moving in response to upstream events.
        info = {
            "trigger": "new_data",
            "session_id": session_id,
            "n_frames": n_frames,
            "n_measurements": n_measurements,
            "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        get_state().set_oracle_last(info)
        self.log_decision("oracle_pass_complete",
                           inputs={"session_id": session_id,
                                    "n_frames": n_frames,
                                    "n_measurements": n_measurements},
                           rationale="Phase-1 stub: counted artifacts, no pipeline yet",
                           session_id=session_id)
        await self.bus.broadcast_event({
            "type": "research_pass",
            "sender": "oracle",
            "kind": "new_data",
            "session_id": session_id,
            "summary": (f"Reviewed session {session_id}: {n_frames or 0} frames, "
                          f"{n_measurements or 0} measurements."),
            "sent_at": info["at"],
        })

    async def _idle_research(self, *, reason: str) -> None:
        """Periodic background pass. Counts recent activity so the dashboard
        sees the Oracle alive between NEW_DATA triggers."""
        cutoff = datetime.utcnow() - timedelta(days=7)
        with get_session() as s:
            n_frames_7d = s.query(Frame).filter(Frame.captured_at >= cutoff).count()
            n_meas_7d = s.query(Measurement).filter(Measurement.epoch_utc >= cutoff).count()

        info = {
            "trigger": reason,
            "n_frames_last_7d": n_frames_7d,
            "n_measurements_last_7d": n_meas_7d,
            "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        get_state().set_oracle_last(info)
        await self.bus.broadcast_event({
            "type": "research_pass",
            "sender": "oracle",
            "kind": "idle_scan",
            "summary": (f"Background scan: {n_frames_7d} frames + "
                          f"{n_meas_7d} measurements in the last 7 days."),
            "sent_at": info["at"],
        })
        # TODO Phase 2: re-evaluate dormant knowledge threads against new data,
        # check the research agenda for upcoming time-critical events,
        # propose candidate targets to Planner via CANDIDATE_TARGET messages.

    async def safe_mode_step(self) -> None:
        await asyncio.sleep(60)
