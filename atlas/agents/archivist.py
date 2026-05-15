"""Archivist agent — post-session processing, reports, memory."""
from __future__ import annotations

import asyncio

from atlas.agents.base import BaseAgent
from atlas.db.models import AgentMessageKind, AgentName


class Archivist(BaseAgent):
    name = AgentName.ARCHIVIST

    async def run(self) -> None:
        self.log.info("Archivist agent online — awaits POST_SESSION triggers")
        while not self.should_stop:
            msg = await self.recv_with_timeout(timeout_s=30.0)
            if msg is None:
                continue
            if msg.kind == AgentMessageKind.POST_SESSION:
                await self._process_session(msg)
            else:
                self.log.debug("Archivist ignoring kind: %s", msg.kind)

    async def _process_session(self, msg) -> None:
        session_id = msg.session_id
        self.log.info("Processing session %s", session_id)
        # TODO Phase 2: full pipeline —
        #   1. calibrate every science frame
        #   2. stack per workflow (Siril / AutoStakkert!4)
        #   3. plate-solve, validate FITS headers, populate WCS
        #   4. per-workflow measurement extraction
        #   5. queue eligible measurements as Submissions (status=QUEUED)
        #   6. render HTML session report
        #   7. notify Oracle of new data
        self.log_decision("session_processed",
                           inputs={"session_id": session_id},
                           rationale="Stub processing acknowledged",
                           session_id=session_id)

        await self.send(
            AgentName.ORACLE, AgentMessageKind.NEW_DATA,
            payload={"session_id": session_id},
            session_id=session_id,
        )

    async def safe_mode_step(self) -> None:
        await asyncio.sleep(60)
