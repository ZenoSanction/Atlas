"""AgentCoordinator — lifecycle manager for all five agents.

Started by ``atlas.server`` at FastAPI app startup. Holds an asyncio task per
agent. Provides graceful shutdown and a status snapshot for the dashboard.
"""
from __future__ import annotations

import asyncio
from typing import Any

from atlas.agents.archivist import Archivist
from atlas.agents.base import BaseAgent
from atlas.agents.critic import Critic
from atlas.agents.operator import Operator
from atlas.agents.oracle import Oracle
from atlas.agents.planner import Planner
from atlas.db.models import AgentName
from atlas.logging_setup import get_logger

log = get_logger("agents.coordinator")


class AgentCoordinator:
    def __init__(self) -> None:
        self._agents: dict[AgentName, BaseAgent] = {
            AgentName.PLANNER:   Planner(),
            AgentName.CRITIC:    Critic(),
            AgentName.OPERATOR:  Operator(),
            AgentName.ARCHIVIST: Archivist(),
            AgentName.ORACLE:    Oracle(),
        }
        self._tasks: dict[AgentName, asyncio.Task] = {}

    async def start_all(self) -> None:
        log.info("Starting all 5 agents...")
        for name, agent in self._agents.items():
            self._tasks[name] = asyncio.create_task(agent.start(), name=f"agent-{name.value}")
        log.info("All agents started.")

    async def stop_all(self) -> None:
        log.info("Stopping all agents...")
        for agent in self._agents.values():
            agent.stop()
        for name, task in self._tasks.items():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        log.info("All agents stopped.")

    def status(self) -> dict[str, Any]:
        out = {}
        for name, agent in self._agents.items():
            task = self._tasks.get(name)
            running = task is not None and not task.done()
            out[name.value] = {
                "running": running,
                "safe_mode": agent.safe_mode,
            }
        return out

    def get(self, name: AgentName) -> BaseAgent:
        return self._agents[name]


_coordinator: AgentCoordinator | None = None


def get_coordinator() -> AgentCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = AgentCoordinator()
    return _coordinator
