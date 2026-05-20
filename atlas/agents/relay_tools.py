"""Inter-agent relay tools — let any agent hand work to another over the bus.

Until now agents only sent messages from inside their own hardcoded loop
logic (Critic → Operator with a weather assessment, Archivist → Oracle on
session end, etc.). When an operator chatted with the Planner and the
Planner wanted to *deliberately* send a plan to the Critic for review,
there was no tool to do it.

This module gives every agent a `send_to_agent(recipient, kind, summary,
payload?)` tool, auto-registered by ``BaseAgent.__init__``. Calls go
through the existing ``AgentBus.send`` path so they:

  - persist to the ``agent_messages`` audit table
  - land in the recipient agent's asyncio queue (Critic now drains its
    queue too, see ``agents/critic.py``)
  - mirror into the Mission Control message-flow ring buffer
  - broadcast to all dashboard subscribers

The recipient processes the message through its existing ``_handle`` (or
the new ``_handle_relayed`` fallback) and can act, ignore, or escalate.

Canonical chain of command for relays:
  Planner   → Critic    : "plan ready, please review the weather fit"
  Critic    → Operator  : "weather assessment, please decide GO/NO-GO"
  Oracle    → Planner   : "candidate target proposal, please schedule"
  Operator  → Planner   : "revision needed (conditions changed)"
  Operator  → Archivist : "session ended, please archive"
  Archivist → Oracle    : "new data available for research pass"
"""
from __future__ import annotations

from datetime import datetime

from atlas.agents.base import ToolSpec
from atlas.db.models import AgentMessageKind, AgentName


# Friendly human-facing notes for each message kind, surfaced in the tool
# description so the model picks the right kind for the situation.
_KIND_HINTS = {
    AgentMessageKind.ALERT.value:
        "a problem the recipient (usually Operator) must consider",
    AgentMessageKind.REVISION_REQUEST.value:
        "ask the Planner to rebuild the plan",
    AgentMessageKind.POST_SESSION.value:
        "tell the Archivist a session has just ended",
    AgentMessageKind.NEW_DATA.value:
        "tell the Oracle new frames/measurements are available",
    AgentMessageKind.CANDIDATE_TARGET.value:
        "propose a target (typically Oracle → Planner)",
    AgentMessageKind.STATUS.value:
        "status update / hand-off summary (default for chat-initiated relays)",
    AgentMessageKind.DECISION.value:
        "record a decision for downstream visibility",
    AgentMessageKind.OPERATOR_COMMAND.value:
        "human-issued command; agents normally shouldn't emit this",
}


def make_relay_tools(agent) -> list[ToolSpec]:
    """Return relay tools bound to a specific agent instance."""

    own = agent.name.value
    valid_recipients = [n.value for n in AgentName if n.value != own]
    valid_kinds = [k.value for k in AgentMessageKind]
    kinds_doc = "\n".join(f"  - {k}: {_KIND_HINTS.get(k, '')}" for k in valid_kinds)

    async def _send(p: dict) -> dict:
        recipient = (p.get("recipient") or "").lower().strip()
        kind = (p.get("kind") or "status").lower().strip()
        summary = (p.get("summary") or "").strip()
        if not summary:
            return {"error": "summary is required (one short line)"}
        if recipient == own:
            return {"error": f"can't relay to yourself ({own})"}
        try:
            recipient_enum = AgentName(recipient)
        except ValueError:
            return {"error": f"unknown recipient: {recipient}. "
                              f"Valid: {valid_recipients}"}
        try:
            kind_enum = AgentMessageKind(kind)
        except ValueError:
            return {"error": f"unknown kind: {kind}. Valid: {valid_kinds}"}

        payload_extra = p.get("payload") or {}
        if not isinstance(payload_extra, dict):
            return {"error": "payload must be an object/dict if provided"}
        payload = {
            "summary": summary,
            "from_chat": True,
            "relayed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            **payload_extra,
        }

        await agent.send(recipient_enum, kind_enum, payload=payload)

        # Surface to the dashboard so the relay is visible immediately,
        # not just on the next /api/mission-control poll.
        try:
            await agent.bus.broadcast_event({
                "type": "agent_relay",
                "sender": own,
                "recipient": recipient,
                "kind": kind,
                "summary": summary,
                "sent_at": payload["relayed_at"],
            })
        except Exception:
            pass

        return {
            "ok": True,
            "from": own,
            "to": recipient,
            "kind": kind,
            "summary": summary,
            "message": (f"Relayed to {recipient} as a {kind} message. "
                          "They'll process it on their own loop."),
        }

    return [
        ToolSpec(
            name="send_to_agent",
            description=(
                "Hand work or context to another agent over the bus. "
                "Use this when the operator's request, or your own "
                "reasoning, leads to a task another agent should run. "
                "Examples:\n"
                "  Planner → Critic with kind=status to ask for a "
                "weather review of a freshly built plan.\n"
                "  Critic → Operator with kind=alert when a threshold "
                "is breached mid-session.\n"
                "  Oracle → Planner with kind=candidate_target_proposal "
                "to add a new target.\n"
                "  Operator → Planner with kind=revision_request to "
                "ask for a fresh schedule.\n\n"
                "Message kinds:\n" + kinds_doc + "\n\n"
                "The bus call persists to the audit log, lands in the "
                "recipient's queue, and shows up in the Mission Control "
                "flow column."),
            input_schema={
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "enum": valid_recipients,
                        "description": "Which agent to send to (not yourself).",
                    },
                    "kind": {
                        "type": "string",
                        "enum": valid_kinds,
                        "description": "Message kind. Use 'status' for general "
                                       "chat-initiated hand-offs unless one of "
                                       "the named kinds fits better.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "One-line plain English description "
                                        "of what you're sending and why.",
                    },
                    "payload": {
                        "type": "object",
                        "description": "Optional structured data (plan blob, "
                                        "target details, threshold values, ...). "
                                        "Use sparingly; most relays only need "
                                        "the summary.",
                    },
                },
                "required": ["recipient", "kind", "summary"],
            },
            handler=_send,
        ),
    ]
