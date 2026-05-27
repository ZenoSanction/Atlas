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
        # Initial research pass once at boot
        try:
            await self._idle_research(reason="startup")
        except Exception:
            self.log.exception("Initial idle pass failed")
        self._initial_done = True
        self._last_idle = asyncio.get_event_loop().time()

        # Background periodic research task — wakes infrequently to
        # scan for state transitions (knowledge threads, revisit
        # candidates). The main loop blocks on the bus, no polling.
        periodic_task = asyncio.create_task(self._periodic_research_loop(),
                                              name="oracle-periodic")
        try:
            while not self.should_stop:
                try:
                    msg = await self.recv()
                except (asyncio.CancelledError, RuntimeError):
                    break
                try:
                    await self._dispatch_msg(msg)
                    self._mark_msg_handled(msg, ok=True)
                except Exception as e:
                    self.log.exception("Oracle dispatch failed")
                    self._mark_msg_handled(msg, ok=False,
                                              error=f"{type(e).__name__}: {e}")
        finally:
            periodic_task.cancel()
            try:
                await periodic_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _dispatch_msg(self, msg) -> None:
        """Inner dispatch — keeps the try/except in the outer loop
        simple so lifecycle status (done/failed) is uniformly applied."""
        if msg.kind == AgentMessageKind.NEW_DATA:
            self.set_task(
                f"new data received — session {msg.payload.get('session_id')}",
                state="working")
            await self._handle_new_data(msg)
            self.set_task("research pass complete — standing by",
                              state="idle")
            return
        if (msg.kind == AgentMessageKind.STATUS
              and (msg.payload or {}).get("kind") == "plan_review"
              and (msg.payload or {}).get("phase") == "oracle"
              and (msg.payload or {}).get("review")):
            # Stage 4 — Oracle auto-opens the plan, evaluates each
            # target against the knowledge-thread + frame history for
            # revisit / extended-integration suggestions, then returns
            # the chain to the Planner.
            payload = msg.payload
            plan_preview = (payload.get("review") or {}).get("plan") or {}
            n = len(plan_preview.get("visible_targets") or [])
            self.set_task(
                f"Stage 4/5: Oracle auto-reviewing plan "
                f"({n} target(s)) — revisit + integration",
                state="working",
            )
            try:
                from datetime import datetime as _dt
                await self.bus.broadcast_event({
                    "type": "review_chain_stage",
                    "sender": "oracle", "stage": "4/5", "agent": "oracle",
                    "phase": "oracle",
                    "review_id": payload.get("review_id"),
                    "n_targets": n,
                    "sent_at": _dt.utcnow().isoformat(timespec="seconds") + "Z",
                })
            except Exception:
                pass
            await self._file_revisit_advisories(payload["review"])
            try:
                from atlas.agents.state import get_state
                get_state().set_review_phase(
                    "finalizing", review_id=payload.get("review_id"))
                await self.send(
                    AgentName.PLANNER, AgentMessageKind.STATUS,
                    payload={
                        "summary": ("Oracle review-chain stage complete — "
                                      "chain returning to Planner for FINAL "
                                      "publication."),
                        "kind": "plan_review",
                        "phase": "finalize",
                        "review_id": payload.get("review_id"),
                        "review": payload.get("review"),
                    },
                )
            except Exception:
                self.log.exception("Failed to send chain back to Planner "
                                      "finalize stage")
            return
        if (msg.kind == AgentMessageKind.STATUS
              and (msg.payload or {}).get("kind") == "plan_advisory_request"
              and (msg.payload or {}).get("review")):
            # Legacy parallel-fanout path (no chain) — still supported.
            await self._file_revisit_advisories(msg.payload["review"])
            return
        await self.handle_relayed_message(msg)

    async def _periodic_research_loop(self) -> None:
        """Sleep-and-research idle scan. Sleeps the full interval
        between passes — no polling. Most nights this only fires once
        or twice between active operations."""
        await asyncio.sleep(IDLE_PASS_INTERVAL_S)
        while not self.should_stop:
            try:
                await self._idle_research(reason="periodic")
            except Exception:
                self.log.exception("Idle research pass failed")
            self._last_idle = asyncio.get_event_loop().time()
            from datetime import datetime, timedelta
            nxt = datetime.utcnow() + timedelta(seconds=IDLE_PASS_INTERVAL_S)
            get_state().update_agent_status(
                "oracle",
                next_tick_at=nxt.isoformat(timespec="seconds") + "Z",
                next_tick_kind="research_scan",
            )
            await asyncio.sleep(IDLE_PASS_INTERVAL_S)

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

    async def _file_revisit_advisories(self, review_dict: dict) -> None:
        """Walk the plan's targets and file info-level advisories for any
        with active knowledge threads or low recent integration counts.

        Purely advisory: the plan is already READY. The dashboard
        renders these as "Oracle suggests: revisit M51 (cadence due)"
        annotations the operator can act on or ignore."""
        from atlas.agents.session_workflow import (
            SessionPlanState, Advisory,
        )
        from atlas.db.session import get_session as _db_sess
        from atlas.db.models import (
            Target, KnowledgeThread, Frame, Measurement,
        )
        from datetime import datetime as _dt, timedelta

        review_id = review_dict.get("review_id")
        plan = review_dict.get("plan") or {}
        self.set_task(
            f"filing revisit advisories for plan {review_id}",
            state="working",
        )

        targets = plan.get("visible_targets") or []
        cutoff_recent = _dt.utcnow() - timedelta(days=30)
        now_iso = _dt.utcnow().isoformat(timespec="seconds") + "Z"
        my_advisories: list[Advisory] = []

        with _db_sess() as s:
            for t in targets:
                name = t.get("target_name")
                if not name:
                    continue
                tgt = s.query(Target).filter_by(name=name).first()
                if tgt is None:
                    continue
                active = (s.query(KnowledgeThread)
                            .filter_by(target_id=tgt.id, state="active")
                            .first())
                if active:
                    my_advisories.append(Advisory(
                        kind="oracle", severity="info",
                        message=(f"{name}: active '{active.kind}' research "
                                   "thread; cadence may be due."),
                        source="oracle", at=now_iso, target_name=name,
                    ))
                    continue
                n_meas_30d = (s.query(Measurement)
                                .filter(Measurement.target_id == tgt.id,
                                          Measurement.epoch_utc >= cutoff_recent)
                                .count())
                n_frames_30d = (s.query(Frame)
                                  .filter(Frame.target_id == tgt.id,
                                            Frame.captured_at >= cutoff_recent)
                                  .count())
                if n_meas_30d > 0 and n_frames_30d < 30:
                    my_advisories.append(Advisory(
                        kind="oracle", severity="info",
                        message=(f"{name}: {n_frames_30d} frame(s) / "
                                   f"{n_meas_30d} measurement(s) in last 30 days "
                                   "— consider extending integration."),
                        source="oracle", at=now_iso, target_name=name,
                    ))

        # ---- LLM cognition: Oracle interpretation of plan +
        # accumulated Critic/Operator findings + revisit candidates.
        # Oracle's domain is multi-night strategy: which targets are
        # underrepresented in the campaign, which deserve extended
        # integration, which would benefit from a different filter
        # mix than tonight's plan offers.
        from atlas.config import get_settings as _gs
        if _gs().llm_chain_review_enabled:
            try:
                live_review = get_state().get_session_review() or {}
                prior_advisories = [
                    {"kind": a.get("kind"), "severity": a.get("severity"),
                      "source": a.get("source"), "message": a.get("message")}
                    for a in (live_review.get("advisories") or [])
                ]
                llm_text = await self.think_about_plan(
                    role_prompt=(
                        "You are the Oracle agent — long-arc research "
                        "strategist. Your job is multi-night campaign "
                        "progress, revisit timing, extended-integration "
                        "candidates. The plan + Critic + Operator "
                        "findings are below; deterministic revisit "
                        "checks already filed per-target advisories. "
                        "What's your STRATEGIC read? Examples: 'M51 has "
                        "had only 90 min in 3 weeks — the campaign goal "
                        "is 1200 min; prioritize doubling its dwell', "
                        "'NGC 1234's active transient thread expected a "
                        "follow-up by now — request a revisit slot'."
                    ),
                    plan_context={
                        "targets": [
                            {"name": t.get("target_name"),
                              "workflow": t.get("workflow"),
                              "campaign_name": t.get("campaign_name"),
                              "scheduled_for_min": t.get("scheduled_for_min")}
                            for t in targets[:10]
                        ],
                        "revisit_candidates": [
                            {"kind": a.kind, "message": a.message}
                            for a in my_advisories if a.target_name
                        ],
                        "prior_advisories": prior_advisories[-15:],
                    },
                )
                my_advisories.append(Advisory(
                    kind="llm_review", severity="info",
                    message=f"[Oracle LLM] {llm_text}",
                    source="oracle", at=now_iso,
                ))
            except Exception:
                self.log.exception("Oracle LLM review threw; chain "
                                      "continues with deterministic only")

        # Atomic append — Critic may also be filing advisories on the
        # same plan; the lock-guarded append in shared state lets both
        # land without overwriting each other.
        from dataclasses import asdict
        n_suggestions = len(my_advisories)
        accepted = get_state().append_advisories(
            review_id,
            [asdict(a) for a in my_advisories],
        )
        if not accepted:
            self.log.info("plan %s already rotated; oracle advisories discarded",
                          review_id)
        self.log_decision("oracle_plan_advisories",
                            inputs={"review_id": review_id,
                                      "targets_checked": len(targets)},
                            outputs={"suggestion_count": n_suggestions,
                                       "accepted": accepted},
                            rationale=(f"Filed {n_suggestions} revisit/extension "
                                         "advisory(ies) against the plan"))
        self.set_task(
            f"plan {review_id}: {n_suggestions} revisit advisor"
            f"{'ies' if n_suggestions != 1 else 'y'} filed",
            state="idle",
        )

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
