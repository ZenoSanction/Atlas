"""Channel base class for the notification dispatcher.

Every notification backend (ntfy, email, webhook, future) implements
this interface. Channels are intentionally simple: they declare
whether they're configured, and they know how to send one notification.
Routing logic + severity filtering live in the dispatcher; channels
just deliver.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Notification:
    """A single notification event handed to channels for delivery.

    All channels see the same dict. Each one renders it the way its
    backend wants — ntfy uses the title/message split, email builds
    a multipart MIME with the detail, webhook ships the raw JSON."""
    severity: str            # "info" | "warning" | "critical"
    title: str
    message: str             # one-line summary
    detail: str = ""         # optional longer body (email/webhook use this)
    source: str = "atlas"    # which agent / subsystem fired it
    tags: list[str] | None = None    # extra labels for backends that want them
    sent_at: datetime = None

    def __post_init__(self) -> None:
        if self.sent_at is None:
            self.sent_at = datetime.utcnow()

    def to_jsonable(self) -> dict:
        return {
            "severity": self.severity, "title": self.title,
            "message": self.message, "detail": self.detail,
            "source": self.source,
            "tags": list(self.tags) if self.tags else [],
            "sent_at": self.sent_at.isoformat(timespec="seconds") + "Z",
        }


class Channel(ABC):
    """Backend for one notification destination (ntfy topic, SMTP
    server, webhook URL, etc.)."""

    name: str = ""             # short identifier ("ntfy", "email", "webhook")
    display_name: str = ""     # human-readable for UI ("ntfy.sh", "Email")

    @abstractmethod
    def configured(self) -> bool:
        """Return True if this channel has enough config to actually
        deliver. Dispatcher skips un-configured channels silently."""

    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """Deliver one notification. Returns True on success, False
        on failure. Should log its own errors at warning level —
        the dispatcher doesn't introspect what went wrong."""
