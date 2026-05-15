"""Critic agent — continuous watchdog. Never decides; only reports."""
from __future__ import annotations

import asyncio
from datetime import datetime

from atlas.agents.base import BaseAgent
from atlas.db.managers import AlertManager, ConfigManager, SessionManager
from atlas.db.models import AgentMessageKind, AgentName, AlertSeverity


FAST_LOOP_S = 90
STANDARD_LOOP_S = 300


class Critic(BaseAgent):
    name = AgentName.CRITIC

    def __init__(self) -> None:
        super().__init__()
        self._last_fast = 0.0
        self._last_standard = 0.0
        self._alert_state: dict[str, int] = {}  # code -> consecutive_count

    async def run(self) -> None:
        self.log.info("Critic agent online (fast %ds, standard %ds)",
                       FAST_LOOP_S, STANDARD_LOOP_S)
        while not self.should_stop:
            now = asyncio.get_event_loop().time()
            if now - self._last_fast >= FAST_LOOP_S:
                await self._fast_loop()
                self._last_fast = now
            if now - self._last_standard >= STANDARD_LOOP_S:
                await self._standard_loop()
                self._last_standard = now
            await asyncio.sleep(5)

    async def _fast_loop(self) -> None:
        """Fast loop: guiding, focus, frame quality, camera. Imaging-only."""
        sess = SessionManager.latest()
        if sess is None or sess.state.value not in ("nominal", "warning"):
            return
        # TODO Phase 2: pull live values from PHD2 and NINA, emit alerts on
        # threshold breach. For now this is a heartbeat.
        self.log.debug("fast loop tick")

    async def _standard_loop(self) -> None:
        """Standard loop: weather, calibration, disk, API, power."""
        sess = SessionManager.latest()
        session_id = sess.id if sess else None
        retention = ConfigManager.get_retention()

        # Calibration freshness — Phase 2 will check actual masters
        # Disk space — Phase 2 will use storage.disk
        # Weather — Phase 2 will use weather.openmeteo
        # API health — handled by base agent's safe_mode tracking

        self.log.debug("standard loop tick")

    async def _raise(self, severity: AlertSeverity, code: str, message: str,
                     session_id: int | None = None, data: dict | None = None,
                     escalate_on_repeats: int = 3) -> None:
        """Deduplicate-aware alert raise."""
        prev = self._alert_state.get(code, 0)
        self._alert_state[code] = prev + 1
        # First-time, or escalation, or every N-th repeat
        if prev == 0 or prev == escalate_on_repeats:
            AlertManager.raise_alert(severity, code, message, AgentName.CRITIC,
                                      session_id=session_id, data=data)
            await self.send(
                AgentName.OPERATOR, AgentMessageKind.ALERT,
                payload={"severity": severity.value, "code": code,
                          "message": message, "data": data or {}},
                session_id=session_id,
            )

    def _clear(self, code: str) -> None:
        if code in self._alert_state:
            del self._alert_state[code]

    async def safe_mode_step(self) -> None:
        # Critic continues monitoring even when Claude is unreachable —
        # its core function is sensor reading, not language reasoning.
        await asyncio.sleep(30)
