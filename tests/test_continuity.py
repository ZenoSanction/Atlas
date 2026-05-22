"""Multi-night continuity: per-target progress + campaign completion."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from atlas.campaigns.continuity import (
    campaign_progress, is_campaign_done, next_filter_priority,
    target_progress,
)


def _seed_target(name="M51") -> int:
    from atlas.db.models import Target
    from atlas.db.session import get_session
    with get_session() as s:
        t = Target(name=name, object_type="galaxy",
                    ra_deg=202.47, dec_deg=47.19)
        s.add(t)
        s.flush()
        return t.id


def _seed_campaign(tids: list[int],
                       min_per_filter: dict[str, int]) -> int:
    from atlas.db.models import (
        Campaign, CampaignStatus, CampaignTarget, WorkflowKind,
    )
    from atlas.db.session import get_session
    with get_session() as s:
        c = Campaign(
            name="test",
            workflow=WorkflowKind.DEEPSKY,
            status=CampaignStatus.ACTIVE,
            success_criterion={
                "type": "deep_integration",
                "min_minutes_per_filter": min_per_filter,
                "min_quality_grade": "B",
            },
        )
        s.add(c)
        s.flush()
        for tid in tids:
            s.add(CampaignTarget(campaign_id=c.id, target_id=tid))
        return c.id


def _seed_frame(target_id: int, filter_name: str, exposure_s: float,
                  quality="B"):
    from atlas.db.models import Frame, FrameQuality
    from atlas.db.session import get_session
    with get_session() as s:
        s.add(Frame(
            session_id=1, target_id=target_id,
            captured_at=datetime.utcnow() - timedelta(hours=2),
            file_path="/fake/light.fit",
            frame_type="light", filter_name=filter_name,
            exposure_s=exposure_s, quality=FrameQuality(quality),
        ))


def test_target_progress_sums_per_filter(clean_campaigns):
    tid = _seed_target()
    for _ in range(10):
        _seed_frame(tid, "L", 180)
    for _ in range(5):
        _seed_frame(tid, "R", 120)
    p = target_progress(tid)
    assert p.total_frames == 15
    assert p.minutes_per_filter["L"] == pytest.approx(30.0, abs=0.01)
    assert p.minutes_per_filter["R"] == pytest.approx(10.0, abs=0.01)


def test_quality_filter_excludes_cd(clean_campaigns):
    tid = _seed_target()
    _seed_frame(tid, "L", 180, "A")
    _seed_frame(tid, "L", 180, "B")
    _seed_frame(tid, "L", 180, "C")
    _seed_frame(tid, "L", 180, "D")
    _seed_frame(tid, "L", 180, "ungraded")
    p = target_progress(tid)
    assert p.total_frames == 3
    assert p.excluded_frames == 2


def test_campaign_progress_pct(clean_campaigns):
    tid = _seed_target()
    cid = _seed_campaign([tid], {"L": 60, "R": 30})
    # 30 min L (50%) + 30 min R (100%) -> mean 75%
    for _ in range(10):
        _seed_frame(tid, "L", 180)
    for _ in range(15):
        _seed_frame(tid, "R", 120)
    p = campaign_progress(cid)
    assert p is not None
    assert 74.0 <= p.complete_pct <= 76.0
    assert p.is_done is False
    assert "L" in p.deficit_minutes_per_filter
    assert "R" not in p.deficit_minutes_per_filter


def test_is_done_at_100pct(clean_campaigns):
    tid = _seed_target()
    cid = _seed_campaign([tid], {"L": 5})
    assert is_campaign_done(cid) is False
    for _ in range(6):
        _seed_frame(tid, "L", 60)  # 6 min total
    assert is_campaign_done(cid) is True


def test_next_filter_priority(clean_campaigns):
    tid = _seed_target()
    cid = _seed_campaign([tid], {"L": 60, "Ha": 60, "R": 60})
    # L 30min (50%), Ha 5min (8%), R 60min (100%) — Ha is the deficit
    for _ in range(10):
        _seed_frame(tid, "L", 180)
    for _ in range(5):
        _seed_frame(tid, "Ha", 60)
    for _ in range(20):
        _seed_frame(tid, "R", 180)
    assert next_filter_priority(cid) == "HA"


def test_returns_none_for_unknown_campaign(clean_campaigns):
    assert campaign_progress(99999) is None
