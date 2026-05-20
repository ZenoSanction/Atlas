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
from atlas.agents.state import get_state  # noqa: F401  used by run()
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
        from atlas.agents.oracle_tools import ORACLE_TOOLS
        for spec in ORACLE_TOOLS:
            self.register_tool(spec)

    async def run(self) -> None:
        self.log.info("Oracle agent online — research + transient pipeline")
        self.set_task("oracle online — initial database scan", state="working")
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
                else:
                    from datetime import datetime, timedelta
                    nxt = datetime.utcnow() + timedelta(
                        seconds=max(0, IDLE_PASS_INTERVAL_S - (now - self._last_idle)))
                    get_state().update_agent_status(
                        "oracle",
                        next_tick_at=nxt.isoformat(timespec="seconds") + "Z",
                        next_tick_kind="research_scan",
                    )
                continue
            if msg.kind == AgentMessageKind.NEW_DATA:
                self.set_task(
                    f"new data received — session {msg.payload.get('session_id')}",
                    state="working")
                await self._handle_new_data(msg)
                self.set_task("research pass complete — standing by",
                              state="idle")
            elif (msg.kind == AgentMessageKind.STATUS
                  and (msg.payload or {}).get("phase") == "oracle_query"
                  and (msg.payload or {}).get("review")):
                # Session pipeline phase 4 — review the plan for revisits +
                # extended integrations, then return to the Operator.
                await self._review_for_revisits(msg.payload["review"])
            else:
                await self.handle_relayed_message(msg)

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

    async def _review_for_revisits(self, review_dict: dict) -> None:
        """Phase 4 of the session pipeline: walk the plan's visible
        targets and, for any with active knowledge threads or stale
        recent imaging, propose a revisit / extended integration."""
        from atlas.agents.session_workflow import (
            SessionReview, OracleSuggestion, PHASE_ORACLE_REVIEW,
        )
        from atlas.db.session import get_session as _db_sess
        from atlas.db.models import (
            Target, KnowledgeThread, Frame, Measurement,
        )
        from datetime import datetime as _dt, timedelta

        review = SessionReview.from_jsonable(review_dict)
        self.set_task(f"reviewing plan {review.review_id} for revisits/extensions",
                      state="working")

        targets = review.plan.get("visible_targets") or []
        cutoff_recent = _dt.utcnow() - timedelta(days=30)

        with _db_sess() as s:
            for t in targets:
                name = t.get("target_name")
                if not name:
                    continue
                tgt = s.query(Target).filter_by(name=name).first()
                if tgt is None:
                    # Target unknown to ATLAS yet — no revisit signal possible
                    continue
                # Active knowledge thread → revisit candidate
                active = (s.query(KnowledgeThread)
                            .filter_by(target_id=tgt.id, state="active")
                            .first())
                if active:
                    review.oracle_suggestions.append(OracleSuggestion(
                        target_name=name,
                        reason=(f"active knowledge thread '{active.kind}' — "
                                  "cadence may be due"),
                        priority_bump=10,
                    ))
                    continue
                # Recent measurements but low frame count → extended integration
                n_meas_30d = (s.query(Measurement)
                                .filter(Measurement.target_id == tgt.id,
                                          Measurement.epoch_utc >= cutoff_recent)
                                .count())
                n_frames_30d = (s.query(Frame)
                                  .filter(Frame.target_id == tgt.id,
                                            Frame.captured_at >= cutoff_recent)
                                  .count())
                if n_meas_30d > 0 and n_frames_30d < 30:
                    review.oracle_suggestions.append(OracleSuggestion(
                        target_name=name,
                        reason=(f"only {n_frames_30d} frame(s) in last 30 days "
                                  f"with {n_meas_30d} measurement(s) — extend integration"),
                        priority_bump=5,
                    ))

        n_sug = len(review.oracle_suggestions)
        review.advance(PHASE_ORACLE_REVIEW, "oracle",
                        note=f"{n_sug} revisit/extension suggestion(s)")
        get_state().set_session_review(review.to_jsonable())
        self.log_decision("oracle_session_review",
                            inputs={"review_id": review.review_id,
                                      "targets_checked": len(targets)},
                            outputs={"suggestion_count": n_sug,
                                      "suggestions": [s.target_name for s in review.oracle_suggestions]},
                            rationale=f"Phase-1 stub revisit logic checked "
                                       f"{len(targets)} target(s)")

        await self.send(
            AgentName.OPERATOR, AgentMessageKind.STATUS,
            payload={
                "summary": (f"Reviewed plan {review.review_id} for revisits — "
                              f"{n_sug} suggestion(s)"),
                "phase": PHASE_ORACLE_REVIEW,
                "review": review.to_jsonable(),
                "from_chat": False,
            },
        )
        self.set_task(f"plan {review.review_id} returned to Operator",
                      state="idle")

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
