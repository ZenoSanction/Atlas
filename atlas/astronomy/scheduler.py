"""Time-aware target scheduler for the nightly session.

The Planner used to sort visible targets purely by (priority, altitude-
at-mid-night) and slice the top N. That worked when the dashboard was
just listing candidates, but it ignored two real-world constraints:

  1. A target that's only above the horizon for the first hour of the
     night can't get a full 3-hour dwell — putting it at slot #1 makes
     sense, putting it at slot #3 doesn't.
  2. With a 4-target cap and a 60-min-minimum-dwell policy, fitting 4
     targets sometimes overruns the dark window. The operator's
     stated preference is "fewer targets at full dwell, not more
     targets partially imaged." So when 4 × full-dwell > dark-window,
     auto-drop to 3 (or 2, or 1) until it fits.

This module turns a ranked list of candidates into a time-ordered queue
of scheduled slots, each carrying a start_utc and dwell_min. The
Planner just consumes the result.

Algorithm (depth-over-breadth greedy):
  - Compute each candidate's visibility window inside [dusk, dawn]:
    the (visible_from, visible_until) range when alt >= horizon_alt.
    Targets never above horizon during the night are dropped.
  - Sort candidates by visible_from (when they enter viewing range).
    Priority is the tiebreaker — earlier-rising target wins, with the
    higher-priority target preferred when they rise simultaneously.
  - Walk the dark window forward in time, slotting in candidates that
    are visible at the cursor. At each slot, give the target its full
    preferred dwell unless its visibility window or the remaining
    dark window cuts it short. If less than min_dwell would be
    available, skip the candidate entirely (don't half-image it).
  - Stop when we've hit max_targets or run out of dark window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from atlas.astronomy.visibility import compute_alt_az


# Step size for the rise/set scan. 5 min is precise enough for slot
# planning and cheap enough to scan a 12-hour window for 20 candidates
# in well under 100 ms on a modest CPU.
_SCAN_STEP = timedelta(minutes=5)


@dataclass
class VisibilityWindow:
    """When a target is above the horizon during the dark window.

    For targets that rise mid-night this is a single contiguous span.
    For circumpolar targets above the horizon all night, this equals
    the full dark window. Targets that never rise above the horizon
    return None from compute_visibility_window."""
    visible_from: datetime
    visible_until: datetime
    peak_alt_deg: float          # highest altitude reached during the span

    @property
    def length_minutes(self) -> float:
        return (self.visible_until - self.visible_from).total_seconds() / 60.0


@dataclass
class ScheduledSlot:
    """One target's reserved chunk of imaging time."""
    target: dict                 # the original Planner candidate dict
    start_utc: datetime
    dwell_min: float
    truncated_from_min: Optional[float] = None  # if dwell got cut short
    visibility: Optional[VisibilityWindow] = None

    @property
    def end_utc(self) -> datetime:
        return self.start_utc + timedelta(minutes=self.dwell_min)


@dataclass
class ScheduleResult:
    """Outcome of one scheduling pass."""
    slots: list[ScheduledSlot] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
        # each: {"target": <candidate dict>, "reason": <str>}
    scheduled_total_min: float = 0.0
    dark_window_min: float = 0.0
    fit_strategy: str = "depth"


def compute_visibility_window(target: dict, lat: float, lon: float,
                               horizon_alt: float,
                               dusk: datetime, dawn: datetime
                               ) -> VisibilityWindow | None:
    """Scan [dusk, dawn] for when target alt >= horizon_alt. Returns the
    longest contiguous visible span (covers the meridian-passage case
    where a target dips below horizon briefly during the night, rare at
    typical horizons but mathematically possible)."""
    ra = float(target["ra_deg"]); dec = float(target["dec_deg"])
    best_from: datetime | None = None
    best_until: datetime | None = None
    best_peak = -90.0
    cur_from: datetime | None = None
    cur_peak = -90.0
    cursor = dusk
    while cursor <= dawn:
        alt, _ = compute_alt_az(ra, dec, lat, lon, cursor)
        if alt >= horizon_alt:
            if cur_from is None:
                cur_from = cursor
                cur_peak = alt
            else:
                cur_peak = max(cur_peak, alt)
        else:
            if cur_from is not None:
                span = (cursor - cur_from).total_seconds()
                if best_from is None or span > (best_until - best_from).total_seconds():
                    best_from, best_until, best_peak = cur_from, cursor, cur_peak
                cur_from = None
                cur_peak = -90.0
        cursor += _SCAN_STEP
    # Close out a span that runs to dawn
    if cur_from is not None:
        span = (dawn - cur_from).total_seconds()
        if best_from is None or span > (best_until - best_from).total_seconds():
            best_from, best_until, best_peak = cur_from, dawn, cur_peak
    if best_from is None or best_until is None:
        return None
    return VisibilityWindow(visible_from=best_from,
                              visible_until=best_until,
                              peak_alt_deg=round(best_peak, 1))


def schedule_targets(
    candidates: list[dict],
    *,
    lat: float,
    lon: float,
    horizon_alt: float,
    dusk: datetime,
    dawn: datetime,
    max_targets: int = 4,
    min_dwell_minutes: float = 60.0,
    preferred_dwell_fn: Callable[[dict], float] | None = None,
    fit_strategy: str = "depth",
) -> ScheduleResult:
    """Time-aware greedy scheduler. See module docstring."""
    if preferred_dwell_fn is None:
        def preferred_dwell_fn(t: dict) -> float:
            # If the Planner already padded a workflow plan, use that.
            # Otherwise fall back to min_dwell.
            return max(
                float(t.get("total_integration_min") or 0.0),
                float(min_dwell_minutes),
            )

    dark_window_min = (dawn - dusk).total_seconds() / 60.0
    result = ScheduleResult(dark_window_min=round(dark_window_min, 1),
                              fit_strategy=fit_strategy)

    # Compute visibility for each candidate; drop never-up targets
    enriched: list[tuple[VisibilityWindow, dict]] = []
    for t in candidates:
        if t.get("ra_deg") is None or t.get("dec_deg") is None:
            result.skipped.append({"target": t,
                                     "reason": "no coords"})
            continue
        vw = compute_visibility_window(t, lat, lon, horizon_alt, dusk, dawn)
        if vw is None or vw.length_minutes < min_dwell_minutes:
            result.skipped.append({
                "target": t,
                "reason": (
                    "never above horizon during dark window"
                    if vw is None
                    else f"visible only {vw.length_minutes:.0f} min "
                          f"(< {min_dwell_minutes:.0f} min minimum)"
                ),
            })
            continue
        enriched.append((vw, t))

    # Sort by rise-time, with priority as tiebreaker for simultaneous risers
    enriched.sort(key=lambda pair: (pair[0].visible_from,
                                       -int(pair[1].get("priority", 0))))

    # Greedy walk through the dark window
    cursor = dusk
    placed_count = 0
    used_ids: set[int] = set()

    while cursor < dawn and placed_count < max_targets:
        # Targets available at the current cursor (visible now)
        ready = [(vw, t) for i, (vw, t) in enumerate(enriched)
                  if i not in used_ids
                  and vw.visible_from <= cursor
                  and vw.visible_until > cursor]
        if not ready:
            # Jump cursor forward to the next target's rise time
            future = [(vw, t, i) for i, (vw, t) in enumerate(enriched)
                       if i not in used_ids and vw.visible_from > cursor]
            if not future:
                break
            cursor = min(vw.visible_from for vw, _, _ in future)
            continue

        # Pick the highest-priority target currently available. Ties
        # broken by earliest visible_from (= rose first, will set
        # soonest, so schedule it before something with longer
        # remaining window).
        chosen_vw, chosen_t = max(
            ready,
            key=lambda pair: (int(pair[1].get("priority", 0)),
                                -pair[0].visible_from.timestamp()),
        )
        chosen_idx = next(i for i, (vw, t) in enumerate(enriched)
                            if vw is chosen_vw and t is chosen_t)

        # How long can we image it?
        preferred = float(preferred_dwell_fn(chosen_t))
        max_here = min(
            (chosen_vw.visible_until - cursor).total_seconds() / 60.0,
            (dawn - cursor).total_seconds() / 60.0,
        )
        if max_here < min_dwell_minutes:
            # Not enough usable window for a full minimum dwell.
            # In depth strategy we drop entirely instead of half-imaging.
            result.skipped.append({
                "target": chosen_t,
                "reason": (f"sets at {chosen_vw.visible_until:%H:%M}Z — "
                            f"only {max_here:.0f} min would be available, "
                            f"< {min_dwell_minutes:.0f} min minimum"),
            })
            used_ids.add(chosen_idx)
            continue

        dwell = min(preferred, max_here)
        truncated_from = preferred if dwell < preferred else None

        result.slots.append(ScheduledSlot(
            target=chosen_t,
            start_utc=cursor,
            dwell_min=round(dwell, 1),
            truncated_from_min=(round(truncated_from, 1)
                                  if truncated_from else None),
            visibility=chosen_vw,
        ))
        result.scheduled_total_min += dwell
        cursor = cursor + timedelta(minutes=dwell)
        used_ids.add(chosen_idx)
        placed_count += 1

    # Anything still unused = ranked but couldn't fit
    for i, (vw, t) in enumerate(enriched):
        if i in used_ids:
            continue
        result.skipped.append({
            "target": t,
            "reason": (f"no slot — visible {vw.visible_from:%H:%M}Z-"
                        f"{vw.visible_until:%H:%M}Z but cap of "
                        f"{max_targets} reached"),
        })

    result.scheduled_total_min = round(result.scheduled_total_min, 1)
    return result
