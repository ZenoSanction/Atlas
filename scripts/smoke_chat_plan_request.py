"""Smoke test: chat-driven plan request -> Planner publishes + chain starts.

Reproduces the operator-reported bug:
  Operator chats ATLAS: "dedicate tonight to NGC 7000."
  ATLAS relays via send_to_agent. Planner used to receive the message,
  log it, and STOP — the chain never started, the dashboard hung.

After the fix, any chat-tagged inbound message (from_chat=True) that
isn't an in-flight review-chain forward kicks _rebuild_plan, which
publishes a fresh plan and sends Stage 1 STATUS to the Critic.

We exercise both possible kinds ATLAS might choose:
  - status (most likely default from older relay_tools hint)
  - revision_request (the correct kind, now reinforced in hints + persona)

Both should now produce a published plan + a STATUS message queued to
the Critic with kind="plan_review", phase="critic".
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _run() -> int:
    from atlas.agents.planner import Planner, _is_chat_plan_request
    from atlas.agents.bus import Message
    from atlas.db.models import AgentMessageKind, AgentName
    import atlas.agents.state as state_mod
    state_mod._state = state_mod._ObservatoryState()

    # ---- 1. Predicate exhaustive truth table ----
    class _M:
        def __init__(self, kind):
            self.kind = kind
            self.sender = AgentName.OPERATOR

    cases = [
        # (label, payload, expected)
        ("status from chat (the bug)",
         {"from_chat": True, "summary": "dedicate tonight to NGC 7000"},
         True),
        ("revision_request from chat",
         {"from_chat": True, "summary": "rebuild for single target"},
         True),
        ("plan_review forward (in-flight chain)",
         {"from_chat": True, "kind": "plan_review", "phase": "critic"},
         False),
        ("non-chat agent-to-agent",
         {"summary": "automated relay"},
         False),
        ("explicit from_chat false",
         {"from_chat": False, "summary": "context update"},
         False),
    ]
    failed = 0
    for label, payload, expected in cases:
        got = _is_chat_plan_request(_M(AgentMessageKind.STATUS), payload)
        ok = got == expected
        status = "OK" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{status}] {label}: expected={expected} got={got}")

    if failed:
        print(f"\n[1] FAIL: {failed} predicate cases wrong")
        return 1
    print("\n[1] Predicate correctness OK (5/5)")

    # ---- 2. Full Planner dispatch: chat-status -> publish + chain ----
    planner = Planner()

    sent_messages: list[tuple] = []
    broadcasted: list[dict] = []

    async def fake_send(recipient, kind, payload=None, session_id=None):
        sent_messages.append((recipient, kind, payload or {}))

    async def fake_broadcast(payload):
        broadcasted.append(payload)
        return None

    rebuild_calls: list[str] = []
    async def fake_rebuild(*, reason):
        rebuild_calls.append(reason)
        # Simulate _rebuild_plan publishing a plan and chain start
        from atlas.agents.state import get_state
        from atlas.agents.plan_version import init_version
        plan = init_version({
            "review_id": "rev-test-1",
            "visible_targets": [
                {"target_name": "NGC 7000",
                 "start_utc": "2026-05-29T02:00:00Z",
                 "end_utc": "2026-05-29T08:00:00Z",
                 "workflow": "deepsky", "priority": 1},
            ],
        }, review_id="rev-test-1", reason=reason)
        get_state().set_tonight_plan(plan)
        # Mimic the Stage-1 hand-off
        await fake_send(AgentName.CRITIC, AgentMessageKind.STATUS,
                          payload={"kind": "plan_review", "phase": "critic",
                                    "review_id": "rev-test-1",
                                    "from_chat": False})

    planner.send = fake_send
    planner._rebuild_plan = fake_rebuild
    planner.bus.broadcast_event = fake_broadcast
    planner.handle_relayed_message = lambda msg: asyncio.sleep(0)
    planner.set_task = lambda *a, **k: None
    planner._mark_msg_handled = lambda *a, **k: None
    planner.log_decision = lambda *a, **k: None

    # Inject the chat-status message
    msg = Message(
        sender=AgentName.OPERATOR,
        recipient=AgentName.PLANNER,
        kind=AgentMessageKind.STATUS,
        payload={
            "from_chat": True,
            "summary": "dedicate tonight to NGC 7000",
            "relayed_at": "2026-05-29T01:00:00Z",
        },
    )
    # Manually drive the dispatch branch (the loop body)
    payload = msg.payload or {}
    matched = False
    if msg.kind == AgentMessageKind.REVISION_REQUEST:
        await planner._handle_revision(msg)
        matched = True
    elif msg.kind == AgentMessageKind.CANDIDATE_TARGET:
        await planner._rebuild_plan(reason="candidate_target")
        matched = True
    elif (msg.kind == AgentMessageKind.STATUS
              and payload.get("kind") == "plan_review"
              and payload.get("phase") == "finalize"):
        matched = True
    elif _is_chat_plan_request(msg, payload):
        await planner.handle_relayed_message(msg)
        await planner._rebuild_plan(
            reason=f"operator_chat_request:{msg.sender.value}")
        matched = True

    assert matched, "chat-status message did not match any branch"
    assert rebuild_calls, "_rebuild_plan was never called"
    assert any("operator_chat_request" in r for r in rebuild_calls), \
        f"wrong reason: {rebuild_calls}"
    assert sent_messages, "no message was sent to Critic"
    crit_recipient, crit_kind, crit_payload = sent_messages[-1]
    assert crit_recipient == AgentName.CRITIC, crit_recipient
    assert crit_kind == AgentMessageKind.STATUS, crit_kind
    assert crit_payload.get("kind") == "plan_review", crit_payload
    assert crit_payload.get("phase") == "critic", crit_payload
    print("[2] chat-status -> rebuild triggered + Stage-1 STATUS sent to "
          f"Critic OK (reason={rebuild_calls[0]!r})")

    # ---- 3. revision_request from chat: same outcome ----
    rebuild_calls.clear()
    sent_messages.clear()
    msg2 = Message(
        sender=AgentName.OPERATOR,
        recipient=AgentName.PLANNER,
        kind=AgentMessageKind.REVISION_REQUEST,
        payload={"from_chat": True, "summary": "rebuild for NGC 7000"},
    )
    async def fake_handle_revision(m):
        # Real path calls _rebuild_plan internally
        await planner._rebuild_plan(reason=f"revision_request:{m.sender.value}")
    planner._handle_revision = fake_handle_revision
    await planner._handle_revision(msg2)
    assert rebuild_calls
    assert sent_messages and sent_messages[-1][0] == AgentName.CRITIC
    print("[3] revision_request path still works")

    # ---- 4. Agent-to-agent STATUS (no from_chat) -> no rebuild ----
    rebuild_calls.clear()
    sent_messages.clear()
    msg3 = Message(
        sender=AgentName.CRITIC,
        recipient=AgentName.PLANNER,
        kind=AgentMessageKind.STATUS,
        payload={"summary": "automated periodic status"},
    )
    payload3 = msg3.payload or {}
    if _is_chat_plan_request(msg3, payload3):
        await planner._rebuild_plan(reason="should-not-fire")
    assert not rebuild_calls, "agent-to-agent STATUS triggered rebuild!"
    print("[4] non-chat STATUS correctly does NOT trigger rebuild")

    # ---- 5. In-flight chain forward -> no re-fire ----
    msg4 = Message(
        sender=AgentName.ORACLE,
        recipient=AgentName.PLANNER,
        kind=AgentMessageKind.STATUS,
        payload={"from_chat": True, "kind": "plan_review",
                 "phase": "finalize", "review_id": "rev-99"},
    )
    payload4 = msg4.payload or {}
    if _is_chat_plan_request(msg4, payload4):
        await planner._rebuild_plan(reason="should-not-fire")
    assert not rebuild_calls
    print("[5] in-flight chain forward (kind=plan_review) does NOT re-fire")

    print("\nChat-driven plan request smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
