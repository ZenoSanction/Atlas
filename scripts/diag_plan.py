"""Diagnostic: directly invoke Planner._rebuild_plan and inspect what
ends up in shared state. This bypasses the agent runtime so we can
see the actual flow without timing / async-loop confusion."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> None:
    from atlas.db.seed import initialise_database
    initialise_database()

    from atlas.agents.planner import Planner
    from atlas.agents.state import get_state

    planner = Planner()
    print(f"Planner instantiated: {planner.name}")

    # Before:
    print(f"\nBEFORE: get_tonight_plan() = {get_state().get_tonight_plan()!r}")
    print(f"BEFORE: get_session_review() = "
            f"{(get_state().get_session_review() or {}).get('state', '(none)')!r}")

    # Direct call
    print("\nCalling _rebuild_plan(reason='diagnostic')...")
    try:
        await planner._rebuild_plan(reason="diagnostic")
        print("_rebuild_plan returned cleanly.")
    except Exception as e:
        import traceback
        print(f"_rebuild_plan RAISED: {type(e).__name__}: {e}")
        traceback.print_exc()

    # After:
    plan = get_state().get_tonight_plan()
    review = get_state().get_session_review()
    print(f"\nAFTER: get_tonight_plan() type={type(plan).__name__}")
    if plan:
        print(f"  built_at:           {plan.get('built_at')}")
        print(f"  reason:             {plan.get('reason')}")
        print(f"  active_campaigns:   {plan.get('active_campaigns')}")
        print(f"  visible_targets:    {len(plan.get('visible_targets') or [])}")
        print(f"  blocked_reason:     {plan.get('blocked_reason', '(none)')}")
    else:
        print(f"  PLAN IS NONE — this is the bug")
    print(f"\nAFTER: session_review state="
            f"{(review or {}).get('state', '(none)')}")
    if review and (review.get('advisories') or []):
        print(f"  advisories: {len(review['advisories'])}")
        for a in review['advisories'][:3]:
            print(f"    - [{a.get('severity')}] {a.get('kind')}: "
                    f"{a.get('message','')[:80]}")


if __name__ == "__main__":
    asyncio.run(main())
