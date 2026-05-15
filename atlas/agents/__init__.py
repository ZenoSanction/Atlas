"""ATLAS agent layer.

Five agents share a clear chain of command:

    Human Operator
        │  approves targets, handles hardware exceptions
        ▼
    Operator (final authority on autonomous decisions)
        │  commands ──▶ Planner
        │  receives ──◀ Critic
        │  triggers  ──▶ Archivist
        ▼
    Archivist (post-session processing)
        │  notifies ──▶ Oracle
        ▼
    Oracle (research, anomalies, transient detection)
        │  feeds  ──▶ Planner (next-session candidates)
        │  alerts ──▶ Operator
"""
from atlas.agents.base import BaseAgent
from atlas.agents.bus import AgentBus, Message, get_bus
from atlas.agents.coordinator import AgentCoordinator

__all__ = [
    "BaseAgent",
    "AgentBus",
    "AgentCoordinator",
    "Message",
    "get_bus",
]
