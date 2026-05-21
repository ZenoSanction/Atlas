"""Where are we in the 24-hour observatory cycle?

ATLAS runs continuously. The Planner builds plans, the Critic watches
conditions, the Archivist ingests data — at all hours. But *imaging*
only happens during astronomical dark, and the right behaviour at
2 PM is fundamentally different from the right behaviour at 11 PM.
Rather than scatter "if hour > X" checks across the agents, every
clock-aware decision routes through this one classifier.

Phases (boundaries by sun altitude):

    DAY                sun > -0.833°      no imaging possible for hours;
                                          archive + plan-for-tonight + idle
    EVENING_TWILIGHT  -18°  <  sun  <  -0.833°    in the evening
                                          pre-flight + cool camera + finalize plan
    ASTRO_DARK        sun <= -18°         actively image
    MORNING_TWILIGHT  -18°  <  sun  <  -0.833°    in the morning
                                          wind down, warm camera, archive

The classifier resolves whether twilight is evening or morning by
comparing the current sun altitude derivative (rising/falling) and
the local solar noon — twilight at 5 AM after astronomical dark
is morning; twilight at 8 PM after a daytime is evening.

Use this every time you need to make a clock-aware decision:

    >>> from atlas.astronomy.day_phase import current_phase
    >>> p = current_phase(lat, lon)
    >>> if p.phase == PHASE_ASTRO_DARK:
    ...     # imaging window — proceed normally
    ... elif p.phase == PHASE_EVENING_TWILIGHT:
    ...     # pre-flight: cool camera, finalize tonight's plan
    ... elif p.phase in (PHASE_DAY, PHASE_MORNING_TWILIGHT):
    ...     # post-night or pre-night — no imaging
    ...     print(f"next dark window in {p.minutes_until_next_phase:.0f} min")
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


PHASE_DAY                = "day"
PHASE_EVENING_TWILIGHT   = "evening_twilight"
PHASE_ASTRO_DARK         = "astro_dark"
PHASE_MORNING_TWILIGHT   = "morning_twilight"

# Sun altitude thresholds (degrees)
SUN_HORIZON_DEG       = -0.833    # standard atmospheric refraction
SUN_ASTRO_DARK_DEG    = -18.0     # IAU astronomical twilight

# Display-friendly phase order
ALL_PHASES = [PHASE_DAY, PHASE_EVENING_TWILIGHT,
                PHASE_ASTRO_DARK, PHASE_MORNING_TWILIGHT]


@dataclass
class DayPhase:
    """The state of the day at a particular instant."""
    phase: str
    sun_altitude_deg: float
    is_imaging_window: bool                # True only in ASTRO_DARK
    minutes_until_next_phase: float        # 0+ rough estimate
    next_phase: str
    # Next astronomical dark window (start, end). If we're already in
    # astro dark, this is the *current* window: (dusk_just_past, dawn_ahead).
    # If we're outside dark, it's the upcoming window.
    next_dark_start_utc: Optional[datetime] = None
    next_dark_end_utc: Optional[datetime] = None
    minutes_of_dark_remaining: Optional[float] = None  # only set in ASTRO_DARK

    def to_jsonable(self) -> dict:
        def iso(dt):
            return dt.isoformat(timespec="seconds") + "Z" if dt else None
        return {
            "phase": self.phase,
            "sun_altitude_deg": round(self.sun_altitude_deg, 2),
            "is_imaging_window": self.is_imaging_window,
            "minutes_until_next_phase": round(self.minutes_until_next_phase, 1),
            "next_phase": self.next_phase,
            "next_dark_start_utc": iso(self.next_dark_start_utc),
            "next_dark_end_utc": iso(self.next_dark_end_utc),
            "minutes_of_dark_remaining": (
                round(self.minutes_of_dark_remaining, 1)
                if self.minutes_of_dark_remaining is not None else None
            ),
        }


def current_phase(lat: float, lon: float,
                    now: datetime | None = None) -> DayPhase:
    """Resolve the current day-cycle phase at a site.

    Cheap: two sun_altitude() calls (one for now, one for +5 min) plus
    a night_window() call. Safe to call on every decision point — no
    network IO, no DB."""
    from atlas.astronomy.visibility import sun_altitude, night_window
    now = now or datetime.utcnow()
    alt_now = sun_altitude(lat, lon, now)
    alt_5min = sun_altitude(lat, lon, now + timedelta(minutes=5))
    rising = alt_5min > alt_now

    # Find the next astronomical dark window (-18°). If we're already
    # in astro dark, night_window started from 12 hours ago to capture
    # the dusk that's already happened.
    in_astro_dark = alt_now <= SUN_ASTRO_DARK_DEG
    if in_astro_dark:
        nw = night_window(lat, lon,
                            now - timedelta(hours=12),
                            altitude_deg=SUN_ASTRO_DARK_DEG)
    else:
        nw = night_window(lat, lon, now,
                            altitude_deg=SUN_ASTRO_DARK_DEG)

    next_dark_start = nw[0] if nw else None
    next_dark_end = nw[1] if nw else None
    dark_remaining: Optional[float] = None

    # Resolve the phase
    if in_astro_dark:
        phase = PHASE_ASTRO_DARK
        next_phase = PHASE_MORNING_TWILIGHT
        if next_dark_end:
            dark_remaining = (next_dark_end - now).total_seconds() / 60.0
            until_next = dark_remaining
        else:
            until_next = 0.0
    elif alt_now < SUN_HORIZON_DEG:
        # In twilight. Rising sun = morning, falling = evening.
        if rising:
            phase = PHASE_MORNING_TWILIGHT
            next_phase = PHASE_DAY
            # Estimate minutes until sun rises above horizon
            until_next = _estimate_minutes_to_altitude(
                lat, lon, now, target_alt=SUN_HORIZON_DEG, ascending=True,
            )
        else:
            phase = PHASE_EVENING_TWILIGHT
            next_phase = PHASE_ASTRO_DARK
            until_next = _estimate_minutes_to_altitude(
                lat, lon, now, target_alt=SUN_ASTRO_DARK_DEG, ascending=False,
            )
    else:
        # Sun above horizon — daytime
        phase = PHASE_DAY
        next_phase = PHASE_EVENING_TWILIGHT
        if next_dark_start:
            # Approximate: evening twilight begins ~45-60 min before
            # astro dark depending on latitude. Use the actual dusk
            # crossing of -18° minus a margin to estimate when
            # twilight starts (sun crosses -0.833°).
            until_next = _estimate_minutes_to_altitude(
                lat, lon, now, target_alt=SUN_HORIZON_DEG, ascending=False,
            )
        else:
            until_next = 0.0

    return DayPhase(
        phase=phase, sun_altitude_deg=alt_now,
        is_imaging_window=(phase == PHASE_ASTRO_DARK),
        minutes_until_next_phase=max(0.0, until_next),
        next_phase=next_phase,
        next_dark_start_utc=next_dark_start,
        next_dark_end_utc=next_dark_end,
        minutes_of_dark_remaining=dark_remaining,
    )


def _estimate_minutes_to_altitude(lat: float, lon: float,
                                     now: datetime, *,
                                     target_alt: float,
                                     ascending: bool,
                                     max_minutes: int = 24 * 60) -> float:
    """Scan forward in 5-minute steps until the sun crosses target_alt
    in the requested direction. Returns minutes from `now` to the
    crossing. Capped at 24 h — beyond that the caller is in a polar
    edge case we don't try to be precise about."""
    from atlas.astronomy.visibility import sun_altitude
    step = 5
    cursor = 0
    prev = sun_altitude(lat, lon, now)
    while cursor < max_minutes:
        cursor += step
        cur = sun_altitude(lat, lon, now + timedelta(minutes=cursor))
        if ascending:
            if prev < target_alt <= cur:
                return float(cursor) - step / 2.0
        else:
            if prev > target_alt >= cur:
                return float(cursor) - step / 2.0
        prev = cur
    return float(max_minutes)


def minutes_useful_remaining(phase: DayPhase, *,
                                safe_startup_overhead_min: float = 15.0,
                                ) -> float | None:
    """How many minutes of *usable* imaging time remain in the current
    astronomical dark window, after subtracting the overhead it'd take
    to spin hardware back up to ready state. Returns None if we're not
    currently in astro dark."""
    if phase.minutes_of_dark_remaining is None:
        return None
    return max(0.0, phase.minutes_of_dark_remaining - safe_startup_overhead_min)
