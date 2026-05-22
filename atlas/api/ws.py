"""WebSocket — live event stream for the dashboard.

The dashboard subscribes via /ws/events and receives JSON messages whenever
an agent sends a message, an alert is raised, or a status changes.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from atlas.agents.bus import SUBSCRIBER_CLOSE_SENTINEL, get_bus
from atlas.logging_setup import get_logger

log = get_logger("api.ws")

websocket_router = APIRouter()


@websocket_router.websocket("/ws/events")
async def events_ws(ws: WebSocket) -> None:
    await ws.accept()
    bus = get_bus()
    queue = bus.subscribe()
    log.info("WebSocket connected (now %d subscribers)", len(bus._broadcast_subs))
    try:
        # Initial greeting so the client can confirm the channel
        await ws.send_text(json.dumps({"type": "connected"}))
        while True:
            event = await queue.get()
            # Bus-initiated close: subscriber accumulated too many
            # un-consumed events (client likely abandoned). Bus has
            # already removed us from the broadcast set; here we
            # close the socket and exit.
            if isinstance(event, dict) and event.get("__bus_close__"):
                log.warning("WebSocket dropped by bus: %s",
                              event.get("reason", "(no reason)"))
                try:
                    await ws.close(code=1001,
                                     reason="server: stale subscriber")
                except Exception:
                    pass
                return
            await ws.send_text(json.dumps(event, default=str))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket error")
    finally:
        bus.unsubscribe(queue)
        log.info("WebSocket disconnected")
