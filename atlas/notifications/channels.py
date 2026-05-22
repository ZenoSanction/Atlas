"""Concrete notification channels.

Each channel reads its own config from the database / credential
vault on construction. Adding a new backend = subclass Channel,
implement configured() and send(), register in CHANNEL_REGISTRY
in dispatcher.py.
"""
from __future__ import annotations

import asyncio
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx

from atlas.db.managers import ConfigManager, CredentialManager
from atlas.logging_setup import get_logger
from atlas.notifications.base import Channel, Notification

log = get_logger("notifications.channels")


# ---- ntfy.sh ---------------------------------------------------------------

class NtfyChannel(Channel):
    """Wraps the existing NtfyClient in the Channel interface so the
    dispatcher routes through it like any other backend."""

    name = "ntfy"
    display_name = "ntfy.sh"

    _PRIORITY = {"info": "low", "warning": "default", "critical": "urgent"}

    def __init__(self) -> None:
        cfg = ConfigManager.get_notifications()
        self._server = (cfg.ntfy_server or "https://ntfy.sh").rstrip("/")
        key = cfg.ntfy_topic_credential_key or "ntfy_topic"
        self._topic = CredentialManager.get(key)

    def configured(self) -> bool:
        return bool(self._topic)

    async def send(self, n: Notification) -> bool:
        if not self.configured():
            return False
        headers = {"Priority": self._PRIORITY.get(n.severity, "default"),
                     "Title": n.title}
        if n.tags:
            headers["Tags"] = ",".join(n.tags)
        url = f"{self._server}/{self._topic}"
        body = n.message + (("\n\n" + n.detail) if n.detail else "")
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.post(url, content=body.encode("utf-8"),
                                  headers=headers)
                r.raise_for_status()
                return True
        except httpx.HTTPError as e:
            log.warning("ntfy publish failed: %s", e)
            return False


# ---- Email (SMTP) ----------------------------------------------------------

class EmailSmtpChannel(Channel):
    """Email via SMTP. Credentials stored in vault as a JSON blob
    under the key 'email_smtp':

        {"host": "smtp.gmail.com", "port": 587, "use_tls": true,
         "username": "...", "password": "...",
         "from_addr": "atlas@observatory.example",
         "to_addrs": ["operator@example.com"]}

    Sync smtplib wrapped in asyncio.to_thread so the dispatcher's
    fan-out doesn't block on slow mail servers."""

    name = "email"
    display_name = "Email (SMTP)"

    def __init__(self) -> None:
        raw = CredentialManager.get("email_smtp")
        self._cfg: dict = {}
        if raw:
            try:
                self._cfg = json.loads(raw)
            except (ValueError, TypeError):
                log.warning("email_smtp credential is not valid JSON")

    def configured(self) -> bool:
        return all(self._cfg.get(k) for k in
                    ("host", "port", "from_addr", "to_addrs"))

    async def send(self, n: Notification) -> bool:
        if not self.configured():
            return False
        try:
            await asyncio.to_thread(self._send_blocking, n)
            return True
        except Exception as e:
            log.warning("email send failed: %s", e)
            return False

    def _send_blocking(self, n: Notification) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[ATLAS {n.severity.upper()}] {n.title}"
        msg["From"] = self._cfg["from_addr"]
        to = self._cfg["to_addrs"]
        msg["To"] = ", ".join(to) if isinstance(to, list) else to
        body = n.message + (("\n\n" + n.detail) if n.detail else "")
        body += f"\n\n--\nATLAS Observatory · {n.source} · {n.sent_at}"
        msg.attach(MIMEText(body, "plain"))
        if self._cfg.get("use_tls", True):
            with smtplib.SMTP(self._cfg["host"],
                                int(self._cfg["port"]), timeout=15) as s:
                s.starttls()
                if self._cfg.get("username"):
                    s.login(self._cfg["username"], self._cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(self._cfg["host"],
                                int(self._cfg["port"]), timeout=15) as s:
                if self._cfg.get("username"):
                    s.login(self._cfg["username"], self._cfg["password"])
                s.send_message(msg)


# ---- Webhook (Discord / Slack / Pushover / custom) -------------------------

class WebhookChannel(Channel):
    """Generic HTTP POST of the notification as JSON. Works for
    Discord webhooks, Slack incoming webhooks, Pushover (with the
    right field shape), Home Assistant, n8n, custom dispatchers,
    anything that takes a POST.

    Credential vault entry under 'webhook_url' (the URL). Optional
    'webhook_template' credential as a JSON template string with
    {severity}, {title}, {message}, {detail} placeholders — useful
    when the target expects a specific schema. Without a template,
    we POST the full Notification dict as JSON."""

    name = "webhook"
    display_name = "Webhook"

    def __init__(self) -> None:
        self._url = CredentialManager.get("webhook_url")
        self._template = CredentialManager.get("webhook_template")

    def configured(self) -> bool:
        return bool(self._url)

    async def send(self, n: Notification) -> bool:
        if not self.configured():
            return False
        if self._template:
            try:
                payload = json.loads(self._template.format(
                    severity=n.severity, title=n.title,
                    message=n.message, detail=n.detail,
                    source=n.source,
                ))
            except (ValueError, KeyError) as e:
                log.warning("webhook template malformed (%s); falling back", e)
                payload = n.to_jsonable()
        else:
            payload = n.to_jsonable()
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.post(self._url, json=payload)
                r.raise_for_status()
                return True
        except httpx.HTTPError as e:
            log.warning("webhook POST failed: %s", e)
            return False
