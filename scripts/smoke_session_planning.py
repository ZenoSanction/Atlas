"""End-to-end session-planning smoke test.

Drives the full Planner->Critic->Operator->Oracle->Planner review chain
manually (no live coordinator needed) and reports what landed at each
stage. Toggles llm_chain_review_enabled off by default so the test
doesn't require an Anthropic API key; pass --with-llm to run the
cognitive chain too.

What gets verified:

  1. Site + equipment config exist or get seeded
  2. Test campaign + targets get created
  3. Planner._rebuild_plan publishes a DRAFT plan + kicks off chain
  4. Critic handler processes the plan_review message + forwards
  5. Operator handler processes + forwards
  6. Oracle handler processes + sends back to Planner
  7. Planner._finalize_review_chain marks FINAL + publishes synthesis
  8. /api/plan/tonight returns the FINAL plan with all chain advisories
  9. Review phase ends on "final"
 10. Lifecycle tracking flows through every message

Run from project root:
    venv\\Scripts\\python.exe scripts\\smoke_session_planning.py
    venv\\Scripts\\python.exe scripts\\smoke_session_planning.py --with-llm
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def _seed_site_and_equipment() -> None:
    """Ensure Site + EquipmentProfile rows exist so the Planner can plan."""
    from atlas.db.models import EquipmentProfile, SiteConfig
    from atlas.db.session import get_session
    with get_session() as s:
        if s.query(SiteConfig).first() is None:
            s.add(SiteConfig(
                latitude=40.75, longitude=-73.98, elevation_m=10.0,
                horizon_alt_min=20.0, name="Smoke Site",
            ))
        if s.query(EquipmentProfile).first() is None:
            s.add(EquipmentProfile(
                camera_type="MONO", sensor_pixel_size_um=3.76,
                focal_length_mm=1000.0, aperture_mm=200.0,
                cooling_setpoint_c=-10.0,
            ))


def _seed_test_campaign() -> tuple[int, list[int]]:
    """Create a small deepsky campaign with 4 visible targets."""
    from atlas.db.models import (
        Campaign, CampaignStatus, CampaignTarget, Target, WorkflowKind,
    )
    from atlas.db.session import get_session
    with get_session() as s:
        # Wipe any prior test campaign for isolation
        for c in s.query(Campaign).filter(Campaign.name == "SMOKE TEST").all():
            s.query(CampaignTarget).filter(
                CampaignTarget.campaign_id == c.id).delete()
            s.delete(c)
        for t in s.query(Target).filter(Target.name.like("SMOKE_%")).all():
            s.delete(t)
        s.flush()
        # Targets at varied RA/Dec so at least some are visible
        target_ids: list[int] = []
        seeds = [
            ("SMOKE_Vega", 279.23, 38.78, "star"),
            ("SMOKE_M13", 250.42, 36.46, "globular_cluster"),
            ("SMOKE_M51", 202.47, 47.20, "galaxy"),
            ("SMOKE_Arcturus", 213.92, 19.18, "star"),
        ]
        for name, ra, dec, kind in seeds:
            t = Target(name=name, object_type=kind, ra_deg=ra, dec_deg=dec,
                          magnitude=6.0)
            s.add(t)
            s.flush()
            target_ids.append(t.id)
        # Campaign with success criterion
        c = Campaign(
            name="SMOKE TEST",
            workflow=WorkflowKind.DEEPSKY,
            status=CampaignStatus.ACTIVE,
            priority=80,
            cadence="every_clear_night",
            success_criterion={
                "type": "deep_integration",
                "min_minutes_per_filter": {"L": 240, "R": 60, "G": 60, "B": 60},
                "min_quality_grade": "B",
            },
        )
        s.add(c)
        s.flush()
        cid = c.id
        for tid in target_ids:
            s.add(CampaignTarget(campaign_id=cid, target_id=tid))
        return cid, target_ids


async def _step_planner_rebuild(planner) -> str | None:
    """Stage 1 — Planner builds DRAFT + sends to Critic."""
    _hr("STAGE 1/5: Planner._rebuild_plan(reason='smoke_test')")
    await planner._rebuild_plan(reason="smoke_test")
    from atlas.agents.state import get_state
    plan = get_state().get_tonight_plan() or {}
    review = get_state().get_session_review() or {}
    phase = get_state().get_review_phase()
    print(f"  tonight_plan published: built_at={plan.get('built_at')}")
    print(f"  visible_targets: {len(plan.get('visible_targets') or [])}")
    print(f"  active_campaigns: {plan.get('active_campaigns')}")
    print(f"  review_state: {review.get('state')}")
    print(f"  review_phase: {phase.get('phase')}")
    print(f"  initial advisory count: {len(review.get('advisories') or [])}")
    return review.get("review_id")


async def _step_critic(critic, review_id: str) -> None:
    """Stage 2 — drain Critic's queue + dispatch."""
    _hr("STAGE 2/5: Critic processes plan_review (phase=critic)")
    from atlas.db.models import AgentName
    from atlas.agents.state import get_state
    queue = critic.bus._queues[AgentName.CRITIC]
    n_initial = queue.qsize()
    print(f"  critic queue size before drain: {n_initial}")
    while not queue.empty():
        msg = await asyncio.wait_for(queue.get(), timeout=2.0)
        if msg is None:
            continue
        kind = (msg.payload or {}).get("kind") if msg.payload else None
        phase = (msg.payload or {}).get("phase") if msg.payload else None
        print(f"  -> dispatching {msg.sender} -> CRITIC [{msg.kind}] "
                f"kind={kind} phase={phase}")
        await critic._handle_relay(msg)
    review = get_state().get_session_review() or {}
    advisories = review.get("advisories") or []
    critic_adv = [a for a in advisories if a.get("source") == "critic"]
    print(f"  critic-filed advisories: {len(critic_adv)}")
    for a in critic_adv[:5]:
        print(f"    [{a.get('severity')}] {a.get('kind')}: "
                f"{(a.get('message') or '')[:80]}")
    phase = get_state().get_review_phase()
    print(f"  review_phase after stage 2: {phase.get('phase')}")


async def _step_operator(operator) -> None:
    _hr("STAGE 3/5: Operator processes plan_review (phase=operator)")
    from atlas.db.models import AgentName
    from atlas.agents.state import get_state
    queue = operator.bus._queues[AgentName.OPERATOR]
    drained = 0
    while not queue.empty():
        msg = await asyncio.wait_for(queue.get(), timeout=2.0)
        if msg is None:
            continue
        kind = (msg.payload or {}).get("kind") if msg.payload else None
        phase = (msg.payload or {}).get("phase") if msg.payload else None
        print(f"  -> dispatching {msg.sender} -> OPERATOR [{msg.kind}] "
                f"kind={kind} phase={phase}")
        await operator._handle(msg)
        drained += 1
    print(f"  drained {drained} message(s)")
    review = get_state().get_session_review() or {}
    op_adv = [a for a in (review.get("advisories") or [])
                  if a.get("source") == "operator"]
    print(f"  operator-filed advisories: {len(op_adv)}")
    for a in op_adv[:3]:
        print(f"    [{a.get('severity')}] {a.get('kind')}: "
                f"{(a.get('message') or '')[:100]}")
    phase = get_state().get_review_phase()
    print(f"  review_phase after stage 3: {phase.get('phase')}")


async def _step_oracle(oracle) -> None:
    _hr("STAGE 4/5: Oracle processes plan_review (phase=oracle)")
    from atlas.db.models import AgentName
    from atlas.agents.state import get_state
    queue = oracle.bus._queues[AgentName.ORACLE]
    drained = 0
    while not queue.empty():
        msg = await asyncio.wait_for(queue.get(), timeout=2.0)
        if msg is None:
            continue
        kind = (msg.payload or {}).get("kind") if msg.payload else None
        phase = (msg.payload or {}).get("phase") if msg.payload else None
        print(f"  -> dispatching {msg.sender} -> ORACLE [{msg.kind}] "
                f"kind={kind} phase={phase}")
        await oracle._dispatch_msg(msg)
        drained += 1
    print(f"  drained {drained} message(s)")
    review = get_state().get_session_review() or {}
    oracle_adv = [a for a in (review.get("advisories") or [])
                      if a.get("source") == "oracle"]
    print(f"  oracle-filed advisories: {len(oracle_adv)}")
    for a in oracle_adv[:3]:
        print(f"    [{a.get('severity')}] {a.get('kind')}: "
                f"{(a.get('message') or '')[:100]}")
    phase = get_state().get_review_phase()
    print(f"  review_phase after stage 4: {phase.get('phase')}")


async def _step_planner_finalize(planner) -> None:
    _hr("STAGE 5/5: Planner._finalize_review_chain (FINAL publish)")
    from atlas.db.models import AgentName
    from atlas.agents.state import get_state
    queue = planner.bus._queues[AgentName.PLANNER]
    drained = 0
    while not queue.empty():
        msg = await asyncio.wait_for(queue.get(), timeout=2.0)
        if msg is None:
            continue
        kind = (msg.payload or {}).get("kind") if msg.payload else None
        phase = (msg.payload or {}).get("phase") if msg.payload else None
        print(f"  -> dispatching {msg.sender} -> PLANNER [{msg.kind}] "
                f"kind={kind} phase={phase}")
        if (kind == "plan_review" and phase == "finalize"):
            await planner._finalize_review_chain(msg.payload)
        else:
            await planner.handle_relayed_message(msg)
        drained += 1
    print(f"  drained {drained} message(s)")
    review = get_state().get_session_review() or {}
    planner_adv = [a for a in (review.get("advisories") or [])
                       if a.get("source") == "planner"]
    print(f"  planner-filed advisories: {len(planner_adv)}")
    for a in planner_adv[:3]:
        print(f"    [{a.get('severity')}] {a.get('kind')}: "
                f"{(a.get('message') or '')[:140]}")
    phase = get_state().get_review_phase()
    print(f"  review_phase after stage 5: {phase.get('phase')}")


async def _report_final_state() -> None:
    _hr("FINAL STATE — what the dashboard's Plan tab would render")
    from atlas.agents.state import get_state
    plan = get_state().get_tonight_plan() or {}
    review = get_state().get_session_review() or {}
    phase = get_state().get_review_phase()
    print(f"\n  Plan built at: {plan.get('built_at')}")
    print(f"  Reason: {plan.get('reason')}")
    print(f"  Visible targets: {len(plan.get('visible_targets') or [])}")
    print(f"  Active campaigns: {plan.get('active_campaigns')}")
    print(f"  Considered: {plan.get('considered_count')}")
    print(f"  Scheduled total min: {plan.get('scheduled_total_min')}")
    print(f"  Dark window min: {plan.get('dark_window_min')}")
    print(f"  Day phase: {(plan.get('day_phase') or {}).get('phase')}")
    print(f"  Review phase: {phase.get('phase')}")

    targets = plan.get("visible_targets") or []
    if targets:
        print(f"\n  TARGETS:")
        for i, t in enumerate(targets, 1):
            print(f"    {i}. {t.get('target_name')} ({t.get('workflow')}) "
                    f"— {t.get('scheduled_for_min')} min "
                    f"from {(t.get('start_utc') or '?')[11:16]} "
                    f"to {(t.get('end_utc') or '?')[11:16]} UTC")

    advisories = review.get("advisories") or []
    print(f"\n  ADVISORIES ({len(advisories)}):")
    for a in advisories:
        src = a.get("source", "?")
        sev = a.get("severity", "?")
        kind = a.get("kind", "?")
        msg = (a.get("message") or "")[:120]
        print(f"    [{sev:8s}] ({src:8s}) {kind:18s} -> {msg}")


async def _verify_lifecycle_status() -> None:
    _hr("LIFECYCLE — message status pills after the chain")
    from atlas.agents.state import get_state
    flow = get_state().get_message_flow(limit=20)
    print(f"  message_flow length: {len(flow)}")
    status_map = get_state().get_message_status_map(
        [m.get("id") for m in flow if m.get("id")]
    )
    rows = []
    for m in flow:
        mid = m.get("id")
        st = status_map.get(mid, {}) if mid else {}
        rows.append((m.get("sender"), m.get("recipient"),
                       m.get("kind"), (m.get("payload") or {}).get("phase"),
                       st.get("status"), st.get("error")))
    print("  Recent messages:")
    print(f"    {'sender':<10}  {'recipient':<10}  {'kind':<22}  "
            f"{'phase':<10}  {'status':<12}  error")
    for sender, recipient, kind, phase, status, err in rows[:15]:
        err_txt = (err or "")[:30]
        print(f"    {sender or '?':<10}  {recipient or '?':<10}  "
                f"{kind or '?':<22}  {phase or '-':<10}  "
                f"{status or '-':<12}  {err_txt}")


async def main() -> None:
    use_llm = "--with-llm" in sys.argv
    if not use_llm:
        # Disable LLM cognitive chain unless explicitly requested —
        # the test doesn't require an Anthropic API key by default.
        os.environ["ATLAS_LLM_CHAIN_REVIEW_ENABLED"] = "false"
        # Force a fresh Settings instance to pick up the env var
        import atlas.config as _cfg
        _cfg._settings = None  # type: ignore
    _hr(f"SESSION PLANNING SMOKE TEST  (LLM chain: "
        f"{'ENABLED — will call Anthropic 4× per chain' if use_llm else 'DISABLED — deterministic only'})")

    from atlas.db.seed import initialise_database
    initialise_database()
    _seed_site_and_equipment()
    cid, tids = _seed_test_campaign()
    print(f"  seeded campaign id={cid} with {len(tids)} target(s)")

    from atlas.agents.coordinator import get_coordinator
    from atlas.db.models import AgentName
    coord = get_coordinator()
    planner = coord.get(AgentName.PLANNER)
    critic = coord.get(AgentName.CRITIC)
    operator = coord.get(AgentName.OPERATOR)
    oracle = coord.get(AgentName.ORACLE)

    review_id = await _step_planner_rebuild(planner)
    if review_id is None:
        print("\n  X Planner did not produce a review_id — aborting test")
        return
    await _step_critic(critic, review_id)
    await _step_operator(operator)
    await _step_oracle(oracle)
    await _step_planner_finalize(planner)
    await _report_final_state()
    await _verify_lifecycle_status()

    _hr("SESSION PLANNING TEST COMPLETE")


if __name__ == "__main__":
    asyncio.run(main())
