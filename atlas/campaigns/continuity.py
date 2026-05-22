"""Multi-night continuity: per-target progress + campaign resume logic.

A campaign on a faint galaxy might want 20 hours of L + 5 hours each
of R / G / B before it's "done". That's six clear nights or more. Each
night ATLAS needs to know:

  * How much *good* integration time does this target already have,
    broken out by filter?
  * How does that compare to the campaign's success_criterion?
  * Which filter should tonight prioritize to make up the deficit?
  * When was the most recent visit? (drives cadence enforcement)

This module is the single source of truth for those questions. The
Planner consults it when building tonight's exposure plan; the Oracle
consults it when deciding whether to nominate a target for revisit;
the dashboard surfaces it on the Targets panel so the human sees
progress at a glance.

Success criterion shape (Campaign.success_criterion JSON):
    {
      "type": "deep_integration",
      "min_minutes_per_filter": {"L": 1200, "R": 300, "G": 300, "B": 300},
      "min_quality_grade": "B"      # exclude C/D frames from count
    }

Other types reserved for later:
    "type": "lightcurve"            # photometry/exoplanet: N continuous nights
    "type": "astrometry_pos"        # N position observations
    "type": "visit_count"           # transient: N visits since reference
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from atlas.db.models import (
    Campaign, CampaignStatus, CampaignTarget, Frame, FrameQuality, Target,
)
from atlas.db.session import get_session
from atlas.logging_setup import get_logger

log = get_logger("campaigns.continuity")


# Quality rank for comparison. "B" >= "C" means a target with min_quality
# of B will count B and A but not C/D.
_QUALITY_RANK = {
    FrameQuality.A: 4,
    FrameQuality.B: 3,
    FrameQuality.C: 2,
    FrameQuality.D: 1,
    FrameQuality.UNGRADED: 0,
}


# ---- dataclasses ---------------------------------------------------------

@dataclass
class TargetProgress:
    """How much usable integration time a target has accumulated,
    broken out by filter, plus session-level metadata."""
    target_id: int
    target_name: str
    total_frames: int = 0
    total_minutes: float = 0.0
    minutes_per_filter: dict[str, float] = field(default_factory=dict)
    frames_per_filter: dict[str, int] = field(default_factory=dict)
    last_visit_utc: Optional[datetime] = None
    n_sessions: int = 0
    excluded_frames: int = 0     # below min_quality_grade

    def to_jsonable(self) -> dict:
        return {
            "target_id": self.target_id,
            "target_name": self.target_name,
            "total_frames": self.total_frames,
            "total_minutes": round(self.total_minutes, 1),
            "minutes_per_filter": {k: round(v, 1)
                                       for k, v in self.minutes_per_filter.items()},
            "frames_per_filter": dict(self.frames_per_filter),
            "last_visit_utc": (self.last_visit_utc.isoformat(timespec="seconds") + "Z"
                                  if self.last_visit_utc else None),
            "n_sessions": self.n_sessions,
            "excluded_frames": self.excluded_frames,
        }


@dataclass
class CampaignProgress:
    """Campaign-wide progress: per-target + completion against criterion."""
    campaign_id: int
    campaign_name: str
    targets: list[TargetProgress] = field(default_factory=list)
    success_criterion: dict = field(default_factory=dict)
    complete_pct: float = 0.0       # 0-100, averaged across targets x filters
    is_done: bool = False
    deficit_minutes_per_filter: dict[str, float] = field(default_factory=dict)
    summary: str = ""

    def to_jsonable(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "targets": [t.to_jsonable() for t in self.targets],
            "success_criterion": self.success_criterion,
            "complete_pct": round(self.complete_pct, 1),
            "is_done": self.is_done,
            "deficit_minutes_per_filter": {k: round(v, 1)
                                              for k, v in
                                              self.deficit_minutes_per_filter.items()},
            "summary": self.summary,
        }


# ---- per-target progress -------------------------------------------------

def target_progress(target_id: int, *,
                       min_quality: Optional[FrameQuality] = FrameQuality.B,
                       ) -> TargetProgress:
    """Sum integration time for every usable frame ever captured on
    this target. UNGRADED frames are *included* by default — the
    Archivist grades them after capture, so a freshly-captured frame
    counts immediately. C / D frames are excluded.

    Pass ``min_quality=None`` to count every frame regardless of grade.
    """
    with get_session() as s:
        target = s.get(Target, target_id)
        if target is None:
            return TargetProgress(target_id=target_id, target_name="(unknown)")
        rows = (s.query(Frame.filter_name, Frame.exposure_s,
                            Frame.quality, Frame.captured_at,
                            Frame.session_id)
                  .filter(Frame.target_id == target_id,
                            Frame.frame_type == "light")
                  .all())
        progress = TargetProgress(
            target_id=target_id, target_name=target.name,
        )
        threshold = _QUALITY_RANK.get(min_quality, 0) if min_quality else 0
        sessions_seen: set[int] = set()
        for filter_name, exposure_s, quality, captured_at, session_id in rows:
            rank = _QUALITY_RANK.get(quality, 0)
            # UNGRADED gets a free pass — Archivist hasn't gotten to it yet
            usable = (quality == FrameQuality.UNGRADED
                        or rank >= threshold)
            if not usable:
                progress.excluded_frames += 1
                continue
            minutes = (float(exposure_s or 0.0)) / 60.0
            filt = (filter_name or "L").upper()
            progress.total_frames += 1
            progress.total_minutes += minutes
            progress.minutes_per_filter[filt] = (
                progress.minutes_per_filter.get(filt, 0.0) + minutes
            )
            progress.frames_per_filter[filt] = (
                progress.frames_per_filter.get(filt, 0) + 1
            )
            if captured_at is not None and (
                    progress.last_visit_utc is None
                    or captured_at > progress.last_visit_utc):
                progress.last_visit_utc = captured_at
            if session_id is not None:
                sessions_seen.add(session_id)
        progress.n_sessions = len(sessions_seen)
    return progress


# ---- campaign progress ---------------------------------------------------

def campaign_progress(campaign_id: int) -> Optional[CampaignProgress]:
    """Per-target progress aggregated against the campaign's
    success_criterion. Returns None if the campaign doesn't exist."""
    with get_session() as s:
        camp = s.get(Campaign, campaign_id)
        if camp is None:
            return None
        criterion = camp.success_criterion or {}
        min_quality_str = (criterion.get("min_quality_grade")
                              or "B").upper()
        try:
            min_quality = FrameQuality(min_quality_str)
        except ValueError:
            min_quality = FrameQuality.B
        # target_id list for this campaign
        target_ids = [ct.target_id for ct in
                          s.query(CampaignTarget)
                              .filter_by(campaign_id=campaign_id).all()]
        camp_name = camp.name

    progress = CampaignProgress(
        campaign_id=campaign_id, campaign_name=camp_name,
        success_criterion=criterion,
    )
    for tid in target_ids:
        tp = target_progress(tid, min_quality=min_quality)
        progress.targets.append(tp)

    # Completion math — sum required vs accumulated, per filter
    min_per_filter = criterion.get("min_minutes_per_filter") or {}
    if min_per_filter and progress.targets:
        # Sum minutes by filter across all targets in this campaign
        accumulated: dict[str, float] = {}
        for tp in progress.targets:
            for filt, minutes in tp.minutes_per_filter.items():
                accumulated[filt] = accumulated.get(filt, 0.0) + minutes
        deficit: dict[str, float] = {}
        per_filter_pct: list[float] = []
        for filt, required in min_per_filter.items():
            have = accumulated.get(filt.upper(), 0.0)
            need = float(required)
            if need <= 0:
                continue
            pct = min(100.0, 100.0 * have / need)
            per_filter_pct.append(pct)
            if have < need:
                deficit[filt.upper()] = need - have
        progress.complete_pct = (sum(per_filter_pct) / len(per_filter_pct)
                                    if per_filter_pct else 0.0)
        progress.deficit_minutes_per_filter = deficit
        progress.is_done = (len(deficit) == 0 and
                              progress.complete_pct >= 99.9)
    elif criterion.get("type") == "visit_count":
        # Transient: count distinct sessions across all targets
        total_sessions = sum(t.n_sessions for t in progress.targets)
        needed = int(criterion.get("min_visits") or 3)
        progress.complete_pct = min(100.0, 100.0 * total_sessions / needed)
        progress.is_done = total_sessions >= needed
    else:
        # No criterion -> we can only report what's accumulated
        progress.complete_pct = 0.0
        progress.is_done = False

    progress.summary = _summarize(progress)
    return progress


def _summarize(p: CampaignProgress) -> str:
    if not p.targets:
        return "campaign has no targets assigned yet"
    pct = round(p.complete_pct, 1)
    if p.is_done:
        return f"campaign DONE — {pct}% across criterion"
    deficits = sorted(p.deficit_minutes_per_filter.items(),
                          key=lambda kv: -kv[1])
    if deficits:
        worst = deficits[0]
        return (f"campaign {pct}% complete — biggest gap: "
                  f"{worst[0]} needs {worst[1]:.0f} more min")
    return f"campaign {pct}% complete"


def is_campaign_done(campaign_id: int) -> bool:
    """Cheap shortcut for the Planner: skip targets whose campaign is done."""
    p = campaign_progress(campaign_id)
    return bool(p and p.is_done)


# ---- planner helper ------------------------------------------------------

def next_filter_priority(campaign_id: int) -> Optional[str]:
    """Which filter has the biggest *relative* deficit?

    Returns the filter name (e.g. "L", "Ha") whose current accumulation
    is furthest from the campaign's min target. Returns None when the
    campaign has no quantitative criterion or is already done.

    Used by the Planner: when picking which filters to allocate
    tonight's window to, prioritize this one first."""
    p = campaign_progress(campaign_id)
    if p is None or p.is_done:
        return None
    crit = p.success_criterion or {}
    needed = crit.get("min_minutes_per_filter") or {}
    if not needed:
        return None
    # accumulated across all targets in this campaign
    accumulated: dict[str, float] = {}
    for tp in p.targets:
        for f, m in tp.minutes_per_filter.items():
            accumulated[f] = accumulated.get(f, 0.0) + m
    worst_filter = None
    worst_ratio = float("inf")
    for filt, target in needed.items():
        target = float(target)
        if target <= 0:
            continue
        ratio = accumulated.get(filt.upper(), 0.0) / target
        if ratio < worst_ratio:
            worst_ratio = ratio
            worst_filter = filt.upper()
    return worst_filter


# ---- batch helpers (Planner / dashboard) --------------------------------

def all_active_campaigns_progress() -> list[CampaignProgress]:
    """Return progress reports for every ACTIVE campaign. Cheap enough
    to call from the Planner each rebuild + the dashboard each fetch."""
    with get_session() as s:
        ids = [c.id for c in
                  s.query(Campaign)
                      .filter(Campaign.status == CampaignStatus.ACTIVE).all()]
    out: list[CampaignProgress] = []
    for cid in ids:
        p = campaign_progress(cid)
        if p is not None:
            out.append(p)
    return out
