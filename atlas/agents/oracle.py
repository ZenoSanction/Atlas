"""Oracle agent — research, anomaly detection, transient pipeline."""
from __future__ import annotations

import asyncio

from atlas.agents.base import BaseAgent
from atlas.db.models import AgentMessageKind, AgentName


class Oracle(BaseAgent):
    name = AgentName.ORACLE

    async def run(self) -> None:
        self.log.info("Oracle agent online — research + transient pipeline")
        while not self.should_stop:
            msg = await self.recv_with_timeout(timeout_s=60.0)
            if msg is None:
                # Background research pass when idle
                await self._idle_research()
                continue
            if msg.kind == AgentMessageKind.NEW_DATA:
                await self._handle_new_data(msg)
            else:
                self.log.debug("Oracle ignoring kind: %s", msg.kind)

    async def _handle_new_data(self, msg) -> None:
        session_id = msg.payload.get("session_id")
        self.log.info("New data notification for session %s", session_id)
        # TODO Phase 2:
        #   1. run image subtraction on transient-flagged frames
        #   2. cross-match candidates against Gaia DR3 / Pan-STARRS / MPC
        #   3. queue confirmed candidates as Submissions (TNS, status=QUEUED)
        #   4. update knowledge threads for involved targets
        #   5. surface anomalies as alerts to Operator if appropriate
        self.log_decision("oracle_pass_complete",
                           inputs={"session_id": session_id},
                           rationale="Stub research pass",
                           session_id=session_id)

    async def _idle_research(self) -> None:
        # TODO Phase 2: background trawl —
        #   - re-evaluate dormant knowledge threads against new data
        #   - check research agenda for upcoming time-critical events
        #   - propose candidate targets to Planner
        return

    async def safe_mode_step(self) -> None:
        await asyncio.sleep(60)
