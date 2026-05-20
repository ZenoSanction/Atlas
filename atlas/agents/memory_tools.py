"""Memory tools every agent gets automatically.

When the user says things like "remember that I prefer Bortle 4 sites" or
"the new dew heater is on filter wheel power port 3", the agent calls
``remember`` to persist that fact to the DB. Later — even after a restart
— the agent can ``recall`` it.

Pinned memories are auto-injected into the agent's system prompt on every
chat, so the agent never has to look them up. Non-pinned memories don't
bloat every prompt; the ``recall`` tool surfaces them on demand.

There's also a shared bucket: ``remember(... shared=True)`` writes to a
"shared" agent name that every other agent can see. Useful for facts the
whole observatory should know ("the imaging telescope is 0.2m f/8 RC,
1600mm focal length").
"""
from __future__ import annotations

from atlas.agents.base import ToolSpec
from atlas.db.managers import MemoryManager, SHARED_AGENT


def make_memory_tools(agent) -> list[ToolSpec]:
    """Return memory tools bound to a specific agent instance. Called
    once from BaseAgent.__init__."""

    own_name = agent.name.value

    async def _remember(p: dict) -> dict:
        content = (p.get("content") or "").strip()
        if not content:
            return {"error": "content is required"}
        pinned = bool(p.get("pinned", False))
        shared = bool(p.get("shared", False))
        tags = p.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        target = SHARED_AGENT if shared else own_name
        mid = MemoryManager.add(target, content, tags=tags,
                                  pinned=pinned, source="chat")
        return {
            "id": mid,
            "agent": target,
            "pinned": pinned,
            "shared": shared,
            "tags": tags,
            "content": content,
            "message": (
                f"Remembered as #{mid}"
                + (" (pinned, will appear in system prompt)" if pinned else "")
                + (" — shared with all agents" if shared else "")
                + "."
            ),
        }

    async def _recall(p: dict) -> dict:
        query = (p.get("query") or "").strip()
        limit = int(p.get("limit", 30))
        limit = max(1, min(100, limit))
        if query:
            rows = MemoryManager.search(own_name, query, limit=limit)
        else:
            rows = MemoryManager.list_for(own_name, limit=limit)
        return {
            "query": query or None,
            "count": len(rows),
            "memories": [
                {"id": r.id, "agent": r.agent, "content": r.content,
                  "pinned": bool(r.pinned), "tags": r.tags or [],
                  "created_at": r.created_at.isoformat()}
                for r in rows
            ],
        }

    async def _forget(p: dict) -> dict:
        mid = int(p.get("id", -1))
        if mid < 0:
            return {"error": "id is required"}
        ok = MemoryManager.delete(mid)
        return {"id": mid, "deleted": ok}

    async def _pin(p: dict) -> dict:
        mid = int(p.get("id", -1))
        if mid < 0:
            return {"error": "id is required"}
        pinned = bool(p.get("pinned", True))
        MemoryManager.update(mid, pinned=pinned)
        return {"id": mid, "pinned": pinned}

    return [
        ToolSpec(
            name="remember",
            description=(
                "Persist a fact about the operator, the observatory, "
                "preferences, equipment, or a decision — so you remember "
                "it across restarts and future chats. Set pinned=true for "
                "facts you should always have at the front of mind (these "
                "are injected into your system prompt on every chat; "
                "use sparingly). Set shared=true to make it visible to "
                "all other agents (Planner / Critic / Operator / "
                "Archivist / Oracle)."),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string",
                                  "description": "The fact to remember. One short paragraph at most."},
                    "pinned": {"type": "boolean",
                                 "description": "If true, always include this in your system prompt."},
                    "shared": {"type": "boolean",
                                 "description": "If true, all other agents can also recall it."},
                    "tags": {"type": "array", "items": {"type": "string"},
                              "description": "Optional tags for grouping (e.g. ['equipment','dew_heater'])."},
                },
                "required": ["content"],
            },
            handler=_remember,
        ),
        ToolSpec(
            name="recall",
            description=(
                "List or search your remembered facts. Omit `query` to "
                "list everything you remember (own private memories + "
                "facts in the shared bucket). Pinned memories are "
                "already in your system prompt; this tool gives you "
                "everything else."),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                                "description": "Optional substring to search for (case-insensitive)."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
            handler=_recall,
        ),
        ToolSpec(
            name="forget",
            description=("Delete one remembered fact by its id. "
                          "Use when the operator says to forget something or "
                          "after a fact becomes wrong (e.g., they replaced the camera)."),
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
            handler=_forget,
        ),
        ToolSpec(
            name="pin_memory",
            description=("Pin or unpin a remembered fact. Pinned facts are "
                          "auto-injected into your system prompt on every chat. "
                          "Unpin to free token budget."),
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "pinned": {"type": "boolean"},
                },
                "required": ["id"],
            },
            handler=_pin,
        ),
    ]
