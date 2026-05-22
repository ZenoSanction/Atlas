"""Smoke test for multi-night continuity.

Builds a synthetic campaign with a deep-integration success_criterion,
seeds frames across multiple synthetic sessions, then verifies:
  1. target_progress sums minutes per filter correctly.
  2. C/D quality frames are excluded by default.
  3. campaign_progress reports completion vs criterion.
  4. is_campaign_done flips at 100%.
  5. next_filter_priority returns the filter with biggest deficit.
  6. all_active_campaigns_progress only returns ACTIVE.

Run from project root:
    venv\\Scripts\\python.exe scripts\\smoke_continuity.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def _clean_db() -> None:
    from atlas.db.models import (
        Campaign, CampaignTarget, Frame, Target,
    )
    from atlas.db.session import get_session
    with get_session() as s:
        s.query(Frame).delete()
        s.query(CampaignTarget).delete()
        s.query(Campaign).delete()
        s.query(Target).delete()


def _seed_target(name: str, ra=210.0, dec=54.0) -> int:
    from atlas.db.models import Target
    from atlas.db.session import get_session
    with get_session() as s:
        t = Target(name=name, object_type="galaxy",
                    ra_deg=ra, dec_deg=dec)
        s.add(t)
        s.flush()
        return t.id


def _seed_campaign(name: str, target_ids: list[int],
                       min_minutes_per_filter: dict[str, int],
                       status="active") -> int:
    from atlas.db.models import (
        Campaign, CampaignStatus, CampaignTarget, WorkflowKind,
    )
    from atlas.db.session import get_session
    with get_session() as s:
        c = Campaign(
            name=name,
            workflow=WorkflowKind.DEEPSKY,
            status=(CampaignStatus.ACTIVE if status == "active"
                      else CampaignStatus.PROPOSED),
            priority=70,
            success_criterion={
                "type": "deep_integration",
                "min_minutes_per_filter": min_minutes_per_filter,
                "min_quality_grade": "B",
            },
        )
        s.add(c)
        s.flush()
        for tid in target_ids:
            s.add(CampaignTarget(campaign_id=c.id, target_id=tid))
        return c.id


def _seed_frame(target_id: int, filter_name: str, exposure_s: float,
                  quality_str: str = "B", session_id: int = 1,
                  days_ago: int = 0) -> None:
    from atlas.db.models import Frame, FrameQuality
    from atlas.db.session import get_session
    with get_session() as s:
        f = Frame(
            session_id=session_id, target_id=target_id,
            captured_at=datetime.utcnow() - timedelta(days=days_ago),
            file_path=f"/fake/{target_id}_{filter_name}.fit",
            frame_type="light", filter_name=filter_name,
            exposure_s=exposure_s,
            quality=FrameQuality(quality_str),
        )
        s.add(f)


def test_target_progress() -> None:
    _hr("1. target_progress sums minutes per filter")
    _clean_db()
    from atlas.campaigns.continuity import target_progress
    tid = _seed_target("M51")
    # 10x L 180s = 30 min L
    for _ in range(10):
        _seed_frame(tid, "L", 180, quality_str="B", session_id=1)
    # 5x R 120s = 10 min R
    for _ in range(5):
        _seed_frame(tid, "R", 120, quality_str="B", session_id=1)

    p = target_progress(tid)
    print(f"  total_frames={p.total_frames} total_minutes={p.total_minutes:.1f}")
    print(f"  per_filter: {p.minutes_per_filter}")
    assert p.total_frames == 15
    assert abs(p.minutes_per_filter["L"] - 30.0) < 0.01
    assert abs(p.minutes_per_filter["R"] - 10.0) < 0.01
    assert p.n_sessions == 1


def test_quality_filter() -> None:
    _hr("2. C/D frames excluded; UNGRADED counted")
    _clean_db()
    from atlas.campaigns.continuity import target_progress
    tid = _seed_target("M51")
    _seed_frame(tid, "L", 180, quality_str="A")
    _seed_frame(tid, "L", 180, quality_str="B")
    _seed_frame(tid, "L", 180, quality_str="C")    # excluded
    _seed_frame(tid, "L", 180, quality_str="D")    # excluded
    _seed_frame(tid, "L", 180, quality_str="ungraded")  # counted

    p = target_progress(tid)
    print(f"  total={p.total_frames} excluded={p.excluded_frames}")
    assert p.total_frames == 3   # A + B + UNGRADED
    assert p.excluded_frames == 2


def test_campaign_progress() -> None:
    _hr("3. campaign_progress vs success_criterion")
    _clean_db()
    from atlas.campaigns.continuity import campaign_progress
    tid = _seed_target("M51")
    # Required: 60 min L, 30 min R
    cid = _seed_campaign("M51 deep", [tid],
                            min_minutes_per_filter={"L": 60, "R": 30})
    # We have: 30 min L (50% of 60), 30 min R (100%)
    for _ in range(10):
        _seed_frame(tid, "L", 180)  # 10x180 = 30 min L
    for _ in range(15):
        _seed_frame(tid, "R", 120)  # 15x120 = 30 min R

    p = campaign_progress(cid)
    assert p is not None
    print(f"  summary: {p.summary}")
    print(f"  complete_pct: {p.complete_pct}")
    print(f"  deficit: {p.deficit_minutes_per_filter}")
    print(f"  is_done: {p.is_done}")
    # L=50%, R=100% -> mean 75%
    assert 74.0 <= p.complete_pct <= 76.0
    assert p.is_done is False
    # L should be in deficit, R should not
    assert "L" in p.deficit_minutes_per_filter
    assert "R" not in p.deficit_minutes_per_filter


def test_is_done() -> None:
    _hr("4. is_campaign_done flips at 100% of criterion")
    _clean_db()
    from atlas.campaigns.continuity import is_campaign_done
    tid = _seed_target("M51")
    cid = _seed_campaign("done test", [tid],
                            min_minutes_per_filter={"L": 5})
    # 1 frame x 60s = 1 min — not done
    _seed_frame(tid, "L", 60)
    assert is_campaign_done(cid) is False
    # Add 5 more min worth
    for _ in range(5):
        _seed_frame(tid, "L", 60)
    print(f"  is_done after 6 min L (need 5): {is_campaign_done(cid)}")
    assert is_campaign_done(cid) is True


def test_next_filter_priority() -> None:
    _hr("5. next_filter_priority -> deepest deficit")
    _clean_db()
    from atlas.campaigns.continuity import next_filter_priority
    tid = _seed_target("M51")
    cid = _seed_campaign("priority test", [tid],
                            min_minutes_per_filter={"L": 60, "Ha": 60, "R": 60})
    # 30 min L, 5 min Ha, 60 min R -> Ha most-deficit (8% complete)
    for _ in range(10):
        _seed_frame(tid, "L", 180)
    for _ in range(5):
        _seed_frame(tid, "Ha", 60)
    for _ in range(20):
        _seed_frame(tid, "R", 180)

    next_filt = next_filter_priority(cid)
    print(f"  next priority filter: {next_filt}")
    assert next_filt == "HA"


def test_all_active() -> None:
    _hr("6. all_active_campaigns_progress filters by status")
    _clean_db()
    from atlas.campaigns.continuity import all_active_campaigns_progress
    tid = _seed_target("M51")
    cid_active = _seed_campaign("active", [tid],
                                    min_minutes_per_filter={"L": 60},
                                    status="active")
    cid_prop = _seed_campaign("proposed", [tid],
                                  min_minutes_per_filter={"L": 60},
                                  status="proposed")
    _seed_frame(tid, "L", 60)

    progs = all_active_campaigns_progress()
    ids = [p.campaign_id for p in progs]
    print(f"  active ids: {ids}")
    assert cid_active in ids
    assert cid_prop not in ids


def main() -> None:
    test_target_progress()
    test_quality_filter()
    test_campaign_progress()
    test_is_done()
    test_next_filter_priority()
    test_all_active()
    _hr("ALL SMOKE TESTS PASSED")
    _clean_db()
    print("  (test rows cleaned)")


if __name__ == "__main__":
    main()
