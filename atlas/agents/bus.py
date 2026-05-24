"""Inter-agent message bus.

Each agent has its own asyncio.Queue, **unbounded**: agent-to-agent
messages are never dropped, never blocked. If an agent falls behind
processing its queue, that's a real problem we want to surface, not
hide behind a silent drop. Memory pressure here would be a symptom,
not a cause — at typical ATLAS message rates the queues hold a few
hundred entries at most across an entire night.

The bus also exposes a broadcast pubsub for the dashboard WebSocket
layer. Subscriber queues are also unbounded — operational messages
never get dropped — but a subscriber that falls more than
DEAD_SUBSCRIBER_THRESHOLD messages behind is treated as dead and
disconnected. Healthy subscribers keep every event; failed
subscribers get garbage-collected instead of silently leaking memory.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, AsyncIterator, Optional

from atlas.db.managers import AgentMessageManager
from atlas.db.models import AgentMessageKind, AgentName
from atlas.logging_setup import get_logger

log = get_logger("agents.bus")

# A WebSocket subscriber that's accumulated this many un-consumed
# events is presumed dead (browser tab closed without notifying us,
# network dropped silently, etc.). At 1 event/sec sustained it
# represents ~17 minutes of stale data — well past anything an active
# operator would tolerate. The bus pushes a close sentinel into the
# subscriber's queue; the WebSocket handler in atlas/api/ws.py sees
# the sentinel and drops the socket cleanly.
DEAD_SUBSCRIBER_THRESHOLD = 10_000

# Sentinel pushed into a subscriber queue when the bus decides the
# subscriber is dead. The WebSocket handler watches for this exact
# dict shape and closes the connection on receipt.
SUBSCRIBER_CLOSE_SENTINEL = {"__bus_close__": True,
                               "reason": "subscriber backlog exceeded"}


@dataclass
class Message:
    sender: AgentName
    recipient: AgentName
    kind: AgentMessageKind
    payload: dict = field(default_factory=dict)
    session_id: Optional[int] = None
    sent_at: datetime = field(default_factory=datetime.utcnow)
    # Short unique id so the dashboard can track each message's
    # delivery lifecycle (delivered → processing → done/failed).
    # 12 hex chars = ~48 bits, enough uniqueness for the ~40-entry
    # message-flow ring buffer without bloating event payloads.
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_jsonable(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender.value if hasattr(self.sender, "value") else str(self.sender),
            "recipient": self.recipient.value if hasattr(self.recipient, "value") else str(self.recipient),
            "kind": self.kind.value if hasattr(self.kind, "value") else str(self.kind),
            "payload": self.payload,
            "session_id": self.session_id,
            "sent_at": self.sent_at.isoformat(),
        }


class AgentBus:
    """One unbounded queue per agent + a fan-out broadcast for
    dashboard subscribers (also unbounded, with dead-subscriber GC)."""

    def __init__(self) -> None:
        # Unbounded agent queues. Agent-to-agent messages are never
        # dropped, never blocked. asyncio.Queue() with no maxsize is
        # unlimited per Python docs.
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
        # Mirror to the Mission Control message-flow ring buffer + each
        # agent's sticky inbox/outbox so the dashboard shows pings.
        try:
            from atlas.agents.state import get_state
            st = get_state()
            jsonable = msg.to_jsonable()
            st.push_message_flow(jsonable)
            # Mark "delivered" — message is on the recipient's queue.
            # The recipient's BaseAgent.recv loop will move it to
            # "processing" when it pulls + "done"/"failed" after the
            # handler runs.
            st.set_message_status(msg.id, "delivered",
                                     sender=jsonable["sender"],
                                     recipient=jsonable["recipient"],
                                     kind=jsonable["kind"])
            item = {
                "sender": jsonable["sender"],
                "recipient": jsonable["recipient"],
                "kind": jsonable["kind"],
                "summary": (msg.payload or {}).get("summary", ""),
                "at": jsonable["sent_at"],
            }
            st.push_inbox(jsonable["recipient"], item)
            st.push_outbox(jsonable["sender"], item)
        except Exception:
            pass
        # Fan-out to dashboard subscribers. Each queue is unbounded;
        # if one has accumulated more than DEAD_SUBSCRIBER_THRESHOLD
        # entries the client is presumed gone — drop the subscriber
        # instead of letting it leak memory forever.
        self._broadcast(msg.to_jsonable())

    async def recv(self, agent: AgentName) -> Message:
        return await self._queues[agent].get()

    # --- dashboard pubsub ---------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict]:
        # Unbounded subscriber queue. The dead-subscriber GC in
        # _broadcast() catches abandoned clients without blocking
        # operational messages.
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._broadcast_subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        self._broadcast_subs.discard(q)

    async def broadcast_event(self, event: dict) -> None:
        """Emit a non-agent event to dashboard subscribers (status updates, etc.)."""
        self._broadcast(event)

    def _broadcast(self, event: dict) -> None:
        """Push an event to every dashboard subscriber. Subscribers
        whose queues have grown past the dead threshold are garbage-
        collected — that's how we maintain the no-drop guarantee for
        live subscribers while not leaking memory for failed ones."""
        dead: list[asyncio.Queue[dict]] = []
        for q in list(self._broadcast_subs):
            if q.qsize() >= DEAD_SUBSCRIBER_THRESHOLD:
                dead.append(q)
                continue
            # put_nowait on an unbounded queue can't raise QueueFull,
            # but defensively handle anything weird (e.g. queue
            # already closed by another task).
            try:
                q.put_nowait(event)
            except Exception as e:
                log.warning("broadcast put failed on subscriber: %s", e)
                dead.append(q)
        for q in dead:
            log.warning("Dropping dead WebSocket subscriber "
                          "(qsize=%d ≥ threshold=%d). Client appears "
                          "to have stopped consuming.",
                          q.qsize(), DEAD_SUBSCRIBER_THRESHOLD)
            self._broadcast_subs.discard(q)
            # Push the close sentinel directly (bypass qsize check).
            # The WebSocket handler's loop sees it on its next get()
            # and tears the socket down. If the queue is already full
            # of stale data, this still works — the WS handler reads
            # in FIFO order and will eventually drain to the sentinel.
            try:
                q.put_nowait(SUBSCRIBER_CLOSE_SENTINEL)
            except Exception:
                pass


_bus: AgentBus | None = None


def get_bus() -> AgentBus:
    global _bus
    if _bus is None:
        _bus = AgentBus()
    return _bus
