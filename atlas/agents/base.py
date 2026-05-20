"""ATLAS BaseAgent.

Each agent is an asyncio task. The base class provides:

- Claude API access (with the system prompt loaded from prompts/<name>.md).
- A tool-dispatch loop for agentic flows.
- Inter-agent message receive/send via the AgentBus.
- Decision logging.
- Safe-autonomous fallback when the Claude API is unreachable: each agent
  declares a ``safe_mode_step()`` method that runs deterministic rules.
"""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from atlas.agents.bus import Message, get_bus
from atlas.config import get_settings
from atlas.db.managers import CredentialManager, DecisionManager
from atlas.db.models import AgentMessageKind, AgentName
from atlas.logging_setup import get_logger


PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass
class ToolSpec:
    """A tool the agent can call. Mirrors the Anthropic tool schema."""
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]  # may return a coroutine


class BaseAgent(ABC):
    """Abstract base. Subclasses implement ``run()`` and ``safe_mode_step()``."""

    name: AgentName  # set by subclass

    def __init__(self) -> None:
        self.log = get_logger(f"agents.{self.name.value}")
        self.bus = get_bus()
        self._settings = get_settings()
        self._system_prompt = self._load_system_prompt()
        self._tools: dict[str, ToolSpec] = {}
        self._anthropic_client = None
        self._stop_event = asyncio.Event()
        self._safe_mode = False
        # Every agent gets the same memory tools (remember/recall/forget/pin)
        # so the operator can teach any of them facts that persist across
        # restarts. Registered here so subclasses don't need to repeat it.
        from atlas.agents.memory_tools import make_memory_tools
        for spec in make_memory_tools(self):
            self.register_tool(spec)
        # Every agent also gets the inter-agent relay tool so it can hand
        # work to another agent (Planner → Critic for review, Critic →
        # Operator for a verdict, Oracle → Planner with new targets, ...).
        from atlas.agents.relay_tools import make_relay_tools
        for spec in make_relay_tools(self):
            self.register_tool(spec)
        # Live mission-control state initialised idle. Subclasses call
        # self.set_task(...) when they begin a phase of work.
        from atlas.agents.state import get_state as _get_state
        _get_state().update_agent_status(self.name.value,
                                          current_task="agent online — starting up",
                                          state="working")

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Entry point invoked by the AgentCoordinator."""
        self.log.info("starting (model=%s)", self._settings.claude_model)
        try:
            await self.run()
        except asyncio.CancelledError:
            self.log.info("cancelled")
            raise
        except Exception:
            self.log.exception("crashed in run()")
            raise

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    @abstractmethod
    async def run(self) -> None:
        """Main loop. Implement in subclass."""

    # --- safe-autonomous fallback ------------------------------------------

    @property
    def safe_mode(self) -> bool:
        return self._safe_mode

    def set_safe_mode(self, on: bool) -> None:
        if on and not self._safe_mode:
            self.log.warning("entering safe-autonomous mode")
        elif not on and self._safe_mode:
            self.log.info("leaving safe-autonomous mode")
        self._safe_mode = on

    async def safe_mode_step(self) -> None:
        """Default no-op. Subclasses override with deterministic rules
        for use when Claude API is unreachable."""
        await asyncio.sleep(5)

    # --- Claude API ---------------------------------------------------------

    def _get_anthropic_client(self):
        if self._anthropic_client is not None:
            return self._anthropic_client
        try:
            import anthropic
        except ImportError:
            self.log.error("anthropic package not installed")
            return None
        api_key = CredentialManager.get("anthropic_api_key")
        if not api_key:
            self.log.warning("No Anthropic API key configured")
            return None
        self._anthropic_client = anthropic.Anthropic(api_key=api_key)
        return self._anthropic_client

    async def think(self, user_message: str, *,
                    extra_context: dict | None = None,
                    max_tool_iters: int = 8,
                    persist_history: bool = True) -> str:
        """Send a message to Claude. Returns the final text response.

        Persistent memory:
          - Pinned ``AgentMemory`` rows for this agent (plus shared) are
            appended to the system prompt as a "Persistent facts" block.
          - The most recent N chat turns from ``AgentChatTurn`` are
            prepended to the messages list, so multi-message conversations
            survive server restarts and warm-room device switches.
          - The new user message + final assistant reply are persisted
            after the call, unless ``persist_history=False`` (used by
            internal callers that don't want to pollute the log).

        Handles the tool-use loop: if Claude asks to call a tool, dispatch
        to the handler, send back the result, and loop until Claude returns
        a text-only response.

        On any API error: sets safe_mode=True and returns a fallback message.
        """
        client = self._get_anthropic_client()
        if client is None:
            self.set_safe_mode(True)
            return "[safe-autonomous: Claude API unavailable]"

        from atlas.db.managers import ChatHistoryManager, MemoryManager
        agent_name = self.name.value

        # --- Load pinned memories into the system prompt ----------------
        pinned = MemoryManager.list_for(agent_name, pinned_only=True, limit=50)
        if pinned:
            facts_lines = []
            for m in pinned:
                tag = " [shared]" if m.agent == "shared" else ""
                facts_lines.append(f"- (#{m.id}){tag} {m.content}")
            system_prompt = (
                self._system_prompt
                + "\n\n## Persistent facts you remember\n"
                + "These are pinned to your memory and shown to you on every chat.\n"
                + "If one becomes wrong, call `forget(id)` or unpin it with `pin_memory(id, pinned=false)`.\n\n"
                + "\n".join(facts_lines)
            )
        else:
            system_prompt = self._system_prompt

        # --- Load recent chat history -----------------------------------
        recent_turns = ChatHistoryManager.recent(agent_name, limit=10)
        messages: list[dict] = []
        for turn in recent_turns:
            messages.append({"role": turn.role, "content": turn.content})
        messages.append({"role": "user", "content": user_message})

        tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in self._tools.values()
        ]

        for _ in range(max_tool_iters):
            # Only pass `tools` when non-empty. The Anthropic API rejects
            # `tools: null` with a 400 ("tools: Input should be a valid array"),
            # so omit the kwarg entirely when this agent has no tools.
            create_kwargs: dict[str, Any] = {
                "model": self._settings.claude_model,
                "max_tokens": self._settings.claude_max_tokens,
                "system": system_prompt,
                "messages": messages,
            }
            if tool_defs:
                create_kwargs["tools"] = tool_defs
            try:
                resp = await asyncio.to_thread(
                    client.messages.create,
                    **create_kwargs,
                )
            except Exception as e:
                self.log.warning("Claude API call failed: %s", e)
                self.set_safe_mode(True)
                return f"[safe-autonomous: {type(e).__name__}]"
            self.set_safe_mode(False)

            if resp.stop_reason == "tool_use":
                # Append assistant turn, then dispatch tools
                messages.append({"role": "assistant", "content": resp.content})
                tool_results = []
                for block in resp.content:
                    if getattr(block, "type", None) == "tool_use":
                        result = await self._dispatch_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        })
                messages.append({"role": "user", "content": tool_results})
                continue

            # final text
            text_parts = [b.text for b in resp.content
                          if getattr(b, "type", None) == "text"]
            reply = "\n".join(text_parts).strip()
            if persist_history:
                try:
                    ChatHistoryManager.append(agent_name, "user", user_message)
                    ChatHistoryManager.append(agent_name, "assistant", reply)
                except Exception as e:
                    self.log.warning("Failed to persist chat turn: %s", e)
            return reply

        return "[max tool iterations reached]"

    async def _dispatch_tool(self, name: str, params: dict) -> Any:
        spec = self._tools.get(name)
        if spec is None:
            return {"error": f"unknown tool: {name}"}
        try:
            result = spec.handler(params)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except Exception as e:
            self.log.exception("tool %s failed", name)
            return {"error": str(e)}

    def register_tool(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    # --- inbound relay handler (overridable by subclasses) ----------------

    async def handle_relayed_message(self, msg) -> None:
        """Default handler for inbound bus messages that aren't matched by
        a subclass's specialised dispatch. Logs + updates the live task +
        broadcasts a 'relay received' event so the Mission Control feed
        shows the hand-off completing.

        Subclasses with kind-specific logic (Planner handling
        REVISION_REQUEST, Operator handling ALERT/STATUS, etc.) already
        do their own thing and don't call this. The Critic + any future
        agents that don't have rich dispatch use this as their default."""
        kind = msg.kind.value if hasattr(msg.kind, "value") else str(msg.kind)
        sender = msg.sender.value if hasattr(msg.sender, "value") else str(msg.sender)
        summary = (msg.payload or {}).get("summary", "")
        self.log.info("relay %s ← %s: %s", kind, sender, summary)
        self.set_task(f"received {kind} from {sender}", state="working")
        try:
            from datetime import datetime as _dt
            await self.bus.broadcast_event({
                "type": "agent_relay_received",
                "sender": sender,
                "recipient": self.name.value,
                "kind": kind,
                "summary": summary,
                "received_at": _dt.utcnow().isoformat(timespec="seconds") + "Z",
            })
        except Exception:
            pass

    # --- live mission-control hooks ----------------------------------------

    def set_task(self, task: str, *, state: str = "working",
                 next_tick_at: str | None = None,
                 next_tick_kind: str | None = None) -> None:
        """Declare what this agent is doing right now. Updates shared state
        and broadcasts a 'task' event for the dashboard to render."""
        from atlas.agents.state import get_state
        get_state().update_agent_status(
            self.name.value,
            current_task=task,
            state=state,
            next_tick_at=next_tick_at,
            next_tick_kind=next_tick_kind,
        )
        # Fire-and-forget broadcast; this is sync-callable from anywhere.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.bus.broadcast_event({
                    "type": "agent_task",
                    "sender": self.name.value,
                    "task": task,
                    "state": state,
                    "next_tick_at": next_tick_at,
                    "next_tick_kind": next_tick_kind,
                    "sent_at": __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z",
                }))
        except RuntimeError:
            # No running loop (e.g. unit tests); just persist to state.
            pass

    # --- bus helpers --------------------------------------------------------

    async def send(self, recipient: AgentName, kind: AgentMessageKind,
                   payload: dict | None = None, session_id: int | None = None) -> None:
        await self.bus.send(Message(
            sender=self.name, recipient=recipient, kind=kind,
            payload=payload or {}, session_id=session_id,
        ))

    async def recv(self) -> Message:
        return await self.bus.recv(self.name)

    async def recv_with_timeout(self, timeout_s: float) -> Optional[Message]:
        try:
            return await asyncio.wait_for(self.bus.recv(self.name), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None

    # --- decisions ----------------------------------------------------------

    def log_decision(self, decision_type: str, *,
                     inputs: dict | None = None, outputs: dict | None = None,
                     rationale: str | None = None,
                     session_id: int | None = None) -> int:
        decision_id = DecisionManager.log(
            agent=self.name, decision_type=decision_type,
            inputs=inputs, outputs=outputs, rationale=rationale,
            session_id=session_id,
        )
        # Mirror to live state so Mission Control panels show recent decisions
        # without having to query the DB each refresh.
        from atlas.agents.state import get_state
        from datetime import datetime as _dt
        get_state().push_agent_decision(self.name.value, {
            "id": decision_id,
            "decision_type": decision_type,
            "rationale": rationale,
            "at": _dt.utcnow().isoformat(timespec="seconds") + "Z",
        })
        return decision_id

    # --- prompts ------------------------------------------------------------

    def _load_system_prompt(self) -> str:
        path = PROMPTS_DIR / f"{self.name.value}.md"
        if not path.exists():
            self.log.warning("System prompt missing: %s", path)
            return f"You are the {self.name.value} agent in the ATLAS observatory system."
        return path.read_text(encoding="utf-8")
