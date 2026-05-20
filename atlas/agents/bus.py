"""Inter-agent message bus.

Each agent has its own asyncio.Queue. The bus routes a Message from sender to
recipient and persists it to the ``agent_messages`` table for audit.

The bus also exposes a broadcast pubsub for the dashboard WebSocket layer:
every persisted message is also dispatched to dashboard subscribers in
near-real-time.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, AsyncIterator, Optional

from atlas.db.managers import AgentMessageManager
from atlas.db.models import AgentMessageKind, AgentName
from atlas.logging_setup import get_logger

log = get_logger("agents.bus")


@dataclass
class Message:
    sender: AgentName
    recipient: AgentName
    kind: AgentMessageKind
    payload: dict = field(default_factory=dict)
    session_id: Optional[int] = None
    sent_at: datetime = field(default_factory=datetime.utcnow)

    def to_jsonable(self) -> dict:
        return {
            "sender": self.sender.value if hasattr(self.sender, "value") else str(self.sender),
            "recipient": self.recipient.value if hasattr(self.recipient, "value") else str(self.recipient),
            "kind": self.kind.value if hasattr(self.kind, "value") else str(self.kind),
            "payload": self.payload,
            "session_id": self.session_id,
            "sent_at": self.sent_at.isoformat(),
        }


class AgentBus:
    """One queue per agent + a fan-out broadcast for dashboard subscribers."""

    def __init__(self) -> None:
        self._queues: dict[AgentName, asyncio.Queue[Message]] = {
            name: asyncio.Queue() for name in AgentName
        }
        self._broadcast_subs: set[asyncio.Queue[dict]] = set()

    # --- agent-to-agent -----------------------------------------------------

    async def send(self, msg: Message) -> None:
        """Deliver msg to the recipient's queue. Persists to DB and broadcasts."""
        # Persist
        try:
            AgentMessageManager.log(
                sender=msg.sender, recipient=msg.recipient, kind=msg.kind,
                payload=msg.payload, session_id=msg.session_id,
            )
        except Exception as e:
            log.warning("Failed to persist agent message: %s", e)
        # Deliver
        await self._queues[msg.recipient].put(msg)
        log.debug("BUS %s -> %s [%s]", msg.sender, msg.recipient, msg.kind)
        # Mirror to the Mission Control message-flow ring buffer
        try:
            from atlas.agents.state import get_state
            get_state().push_message_flow(msg.to_jsonable())
        except Exception:
            pass
        # Fan-out to dashboard
        for q in list(self._broadcast_subs):
            try:
                q.put_nowait(msg.to_jsonable())
            except asyncio.QueueFull:
                pass

    async def recv(self, agent: AgentName) -> Message:
        return await self._queues[agent].get()

    # --- dashboard pubsub ---------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._broadcast_subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        self._broadcast_subs.discard(q)

    async def broadcast_event(self, event: dict) -> None:
        """Emit a non-agent event to dashboard subscribers (status updates, etc.)."""
        for q in list(self._broadcast_subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


_bus: AgentBus | None = None


def get_bus() -> AgentBus:
    global _bus
    if _bus is None:
        _bus = AgentBus()
    return _bus
