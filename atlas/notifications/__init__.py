"""Multi-channel notification dispatcher.

Channels are pluggable: each is a class in channels.py implementing
Channel from base.py. Register new ones in dispatcher.CHANNEL_REGISTRY.

Public surface:
    get_dispatcher() — singleton
    Notification     — event dataclass passed to dispatch()
    send_alert(...)  — legacy convenience kept for in-flight callers
"""
from atlas.notifications.base import Channel, Notification
from atlas.notifications.dispatcher import (
    CHANNEL_REGISTRY, NotificationDispatcher, get_dispatcher,
)
from atlas.notifications.ntfy import NtfyClient, send_alert

__all__ = [
    "Channel", "Notification", "NotificationDispatcher",
    "get_dispatcher", "CHANNEL_REGISTRY",
    "NtfyClient", "send_alert",
]
