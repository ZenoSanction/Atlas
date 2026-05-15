"""Planner agent — builds nightly schedules and NINA sequences."""
from __future__ import annotations

import asyncio

from atlas.agents.base import BaseAgent
from atlas.db.models import AgentMessageKind, AgentName


class Planner(BaseAgent):
    name = AgentName.PLANNER

    async def run(self) -> None:
        self.log.info("Planner agent online")
        while not self.should_stop:
            msg = await self.recv_with_timeout(timeout_s=10.0)
            if msg is None:
                continue
            if msg.kind == AgentMessageKind.REVISION_REQUEST:
                await self._handle_revision(msg)
            else:
                self.log.debug("Planner ignoring kind: %s", msg.kind)

    async def _handle_revision(self, msg) -> None:
        self.log.info("Revision requested by %s", msg.sender)
        # TODO Phase 2: build a new plan from current campaigns + conditions,
        # produce NINA sequence XML, and reply with the plan blob.
        self.log_decision("plan_requested", inputs={"details": msg.payload},
                           rationale="Stub plan acknowledgement",
                           session_id=msg.session_id)

    async def safe_mode_step(self) -> None:
        await asyncio.sleep(30)
