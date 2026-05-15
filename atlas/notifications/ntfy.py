"""ntfy.sh push notification client.

Per Round 3 #17: ntfy.sh is the default. Free, self-hostable, supports
priority levels including ``urgent`` for bypass-do-not-disturb.

The topic name is treated as a secret (anyone with the name can read it)
and is therefore stored in the credential vault.
"""
from __future__ import annotations

import httpx

from atlas.db.managers import ConfigManager, CredentialManager
from atlas.logging_setup import get_logger

log = get_logger("notifications.ntfy")


PRIORITY_MAP = {
    "info":     "low",
    "warning":  "default",
    "critical": "urgent",
}


class NtfyClient:
    def __init__(self, server: str = "https://ntfy.sh", topic: str | None = None,
                 timeout: float = 8.0) -> None:
        self._server = server.rstrip("/")
        self._topic = topic
        self._timeout = timeout

    async def publish(self, message: str, *, title: str | None = None,
                       priority: str = "default", tags: list[str] | None = None) -> None:
        if not self._topic:
            log.debug("ntfy publish skipped: no topic configured")
            return
        headers = {"Priority": priority}
        if title:
            headers["Title"] = title
        if tags:
            headers["Tags"] = ",".join(tags)
        url = f"{self._server}/{self._topic}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            try:
                r = await c.post(url, content=message.encode("utf-8"),
                                  headers=headers)
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("ntfy publish failed: %s", e)


async def send_alert(severity: str, title: str, message: str) -> None:
    """High-level helper that pulls topic from credentials and respects
    user notification preferences."""
    cfg = ConfigManager.get_notifications()
    if severity == "info" and not cfg.notify_info:
        return
    if severity == "warning" and not cfg.notify_warning:
        return
    if severity == "critical" and not cfg.notify_critical:
        return
    topic_key = cfg.ntfy_topic_credential_key or "ntfy_topic"
    topic = CredentialManager.get(topic_key)
    if not topic:
        log.debug("send_alert skipped: no ntfy topic stored")
        return
    client = NtfyClient(server=cfg.ntfy_server, topic=topic)
    await client.publish(
        message, title=title,
        priority=PRIORITY_MAP.get(severity, "default"),
        tags=["telescope"] if severity != "critical" else ["telescope", "rotating_light"],
    )
