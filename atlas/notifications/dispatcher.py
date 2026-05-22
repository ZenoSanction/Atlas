"""Central notification dispatcher.

Single entry point — get_dispatcher().dispatch(notification) — fans
the notification out to every configured channel in parallel. Each
channel handles its own errors; one failing backend doesn't block
the others.

Severity routing:
  critical → all enabled channels
  warning  → channels marked warning_too in the notifications config
  info     → never paged (info advisories live on the dashboard only)
"""
from __future__ import annotations

import asyncio
from typing import Type

from atlas.db.managers import ConfigManager
from atlas.logging_setup import get_logger
from atlas.notifications.base import Channel, Notification
from atlas.notifications.channels import (
    EmailSmtpChannel, NtfyChannel, WebhookChannel,
)

log = get_logger("notifications.dispatcher")


# Public registry — extend here to plug a new backend in. New channel
# classes don't need any other touch-up: dispatcher constructs them
# on the fly, the Setup-tab API reads the same dict.
CHANNEL_REGISTRY: dict[str, Type[Channel]] = {
    "ntfy":     NtfyChannel,
    "email":    EmailSmtpChannel,
    "webhook":  WebhookChannel,
}


class NotificationDispatcher:
    """Singleton — use get_dispatcher()."""

    def __init__(self) -> None:
        # Channels are reconstructed on every dispatch so config /
        # credential changes take effect immediately without restart.
        pass

    def channel_status(self) -> list[dict]:
        """Snapshot of every registered channel + whether it's
        currently configured. Setup-tab reads this."""
        out = []
        for key, cls in CHANNEL_REGISTRY.items():
            try:
                inst = cls()
                ok = inst.configured()
            except Exception as e:
                ok = False
                log.warning("channel %s init failed: %s", key, e)
            out.append({
                "name": key,
                "display_name": cls.display_name,
                "configured": ok,
            })
        return out

    async def dispatch(self, notification: Notification) -> dict[str, bool]:
        """Fan out to every configured channel in parallel. Returns
        per-channel result. info severity is dropped silently."""
        if notification.severity == "info":
            log.debug("info notification not paged: %s", notification.title)
            return {}
        # Honor the per-severity toggle in NotificationConfig
        cfg = ConfigManager.get_notifications()
        if notification.severity == "warning" and not cfg.notify_warning:
            log.debug("warning notification suppressed by config")
            return {}
        if notification.severity == "critical" and not cfg.notify_critical:
            log.debug("critical notification suppressed by config")
            return {}

        results: dict[str, bool] = {}
        channels = []
        for key, cls in CHANNEL_REGISTRY.items():
            try:
                inst = cls()
                if inst.configured():
                    channels.append((key, inst))
                else:
                    results[key] = False  # silently skipped — not configured
            except Exception as e:
                log.warning("channel %s init failed: %s", key, e)
                results[key] = False
        if not channels:
            log.info("notification dispatched but no channels configured: %s",
                       notification.title)
            return results
        # Fire all channels in parallel; collect results
        coros = [self._send_one(inst, notification) for _, inst in channels]
        outcomes = await asyncio.gather(*coros, return_exceptions=True)
        for (key, _), out in zip(channels, outcomes):
            if isinstance(out, Exception):
                log.warning("channel %s raised: %s", key, out)
                results[key] = False
            else:
                results[key] = bool(out)
        log.info(
            "Notification [%s] %s → %s",
            notification.severity, notification.title,
            ", ".join(f"{k}={'ok' if v else 'fail'}"
                        for k, v in results.items()) or "(no channels)",
        )
        return results

    async def _send_one(self, inst: Channel, n: Notification) -> bool:
        try:
            return await inst.send(n)
        except Exception as e:
            log.warning("channel %s send raised: %s", inst.name, e)
            return False


_dispatcher: NotificationDispatcher | None = None


def get_dispatcher() -> NotificationDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = NotificationDispatcher()
    return _dispatcher
