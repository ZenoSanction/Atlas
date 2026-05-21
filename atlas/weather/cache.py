"""Process-wide weather cache.

The old design had three different agents each pulling Open-Meteo on
their own schedule — Critic every 5 minutes, Operator's pre-flight
every 2 minutes, Planner on every rebuild. That meant ~30 unnecessary
HTTP round-trips per hour, each one able to slow the agent that
triggered it by 0.5-2 s. Worse: agents were waking up just to do work
they didn't need to do.

This module centralises the pull. One snapshot lives in process memory
with a "pulled_at" timestamp. Every consumer hits the cache, not the
network:

  * Critic's standard loop wakes only when something else does
    (session start, weather alert from elsewhere, operator command).
    When it does check, it asks for the cached snapshot — which is
    refreshed transparently if it's older than the TTL.
  * Operator's pre-flight reads the cached snapshot without
    triggering a refresh. The pre-flight gate just reports "weather
    OK" or "stale" based on cache freshness.
  * Planner — and only the Planner — passes ``force_refresh=True``
    before building a new session plan, because that's the one moment
    fresh data actually matters for the next 8 hours of operations.

Concurrency: a single asyncio.Lock serialises in-flight refreshes so
two consumers racing to refresh a stale cache only generate one
network call; the second waits for the first's result.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from atlas.logging_setup import get_logger
from atlas.weather.openmeteo import OpenMeteoClient, WeatherSnapshot

log = get_logger("weather.cache")


# ---- Polling modes ---------------------------------------------------------
# The cache exposes a polling-mode dial so monitoring tightens up when
# conditions matter and relaxes when nothing's happening. The Critic
# sets the mode each tick based on session state + verdict + metric
# trend.
#
#   IDLE        — no session, day phase or clearly nominal weather.
#                  Open-Meteo's "current" only updates roughly once
#                  per hour at the source anyway, so polling more often
#                  during the day burns API quota for no signal.
#   ACTIVE      — a session is running normally. We need fresh weather
#                  fast enough to catch storms forming during the night.
#   BORDERLINE  — at least one metric is within 20% of its critical
#                  threshold, OR the verdict is CAUTION. Tighten further.
MODE_IDLE       = "idle"
MODE_ACTIVE     = "active"
MODE_BORDERLINE = "borderline"

# Per-mode TTL (seconds). Tuned for Open-Meteo's free tier (≤ 10k
# requests/day): 60s borderline × 1 site = ~1440 requests/day even if
# we stayed pinned in that mode for 24 h, well inside the limit.
TTL_BY_MODE = {
    MODE_IDLE:       900,    # 15 min — was the old default
    MODE_ACTIVE:      90,    # 1.5 min — fast enough to catch storms
    MODE_BORDERLINE:  60,    # 1 min — tightest we go
}

# Legacy alias still referenced by older imports
DEFAULT_TTL_S = TTL_BY_MODE[MODE_IDLE]


@dataclass
class WeatherCacheState:
    """Outcome of a cache read, including freshness signal."""
    snapshot: Optional[WeatherSnapshot]
    pulled_at: Optional[datetime]
    age_seconds: Optional[float]
    fresh: bool                # True if age <= ttl
    refreshed_this_call: bool  # True if we just hit the network
    forecast_hours: list[dict] | None = None
    mode: str = MODE_IDLE      # what polling mode the cache is in
    ttl_s: int = 900           # current TTL — derived from mode


class WeatherCache:
    """Singleton cache. Use ``get_weather_cache()`` to access — there's
    one instance per process, lazily constructed on first call.

    Polling mode (IDLE / ACTIVE / BORDERLINE) controls the TTL, set by
    the Critic each tick. The cache itself doesn't decide when to be
    "active" — that's a policy decision that lives upstream where
    session + verdict state is known."""

    def __init__(self, ttl_s: int | None = None) -> None:
        # ttl_s arg retained for tests / explicit overrides. When None
        # we follow the polling-mode dial.
        self._mode: str = MODE_IDLE
        self._ttl_override_s: int | None = ttl_s
        self._snapshot: Optional[WeatherSnapshot] = None
        self._pulled_at: Optional[datetime] = None
        self._forecast_hours: list[dict] | None = None
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def ttl_s(self) -> int:
        if self._ttl_override_s is not None:
            return self._ttl_override_s
        return TTL_BY_MODE.get(self._mode, DEFAULT_TTL_S)

    def set_mode(self, mode: str) -> str:
        """Switch polling tightness. Returns the previous mode so
        callers can detect transitions and log them."""
        if mode not in TTL_BY_MODE:
            log.warning("Unknown polling mode %r — staying in %s",
                         mode, self._mode)
            return self._mode
        prev = self._mode
        if mode != prev:
            log.info("Weather polling mode %s → %s (TTL %d s → %d s)",
                      prev, mode, TTL_BY_MODE[prev], TTL_BY_MODE[mode])
        self._mode = mode
        return prev

    def age_seconds(self) -> Optional[float]:
        if self._pulled_at is None:
            return None
        return (datetime.utcnow() - self._pulled_at).total_seconds()

    def is_stale(self) -> bool:
        age = self.age_seconds()
        if age is None:
            return True
        return age > self.ttl_s

    def peek(self) -> WeatherCacheState:
        """Non-blocking snapshot of current cache state. Never pulls."""
        age = self.age_seconds()
        return WeatherCacheState(
            snapshot=self._snapshot, pulled_at=self._pulled_at,
            age_seconds=age, fresh=not self.is_stale(),
            refreshed_this_call=False,
            forecast_hours=self._forecast_hours,
            mode=self._mode, ttl_s=self.ttl_s,
        )

    async def get(self, *, lat: float, lon: float,
                    force_refresh: bool = False,
                    forecast_hours: int = 12) -> WeatherCacheState:
        """Return a cache state, refreshing in-place if stale or forced.

        Locks during refresh so concurrent callers don't double-hit
        Open-Meteo. If the network call fails, the previous cache
        (even if stale) is returned with a warning logged."""
        if not force_refresh and not self.is_stale():
            return self.peek()
        async with self._lock:
            # Re-check inside the lock — another coroutine may have
            # refreshed while we were waiting on the lock.
            if not force_refresh and not self.is_stale():
                return self.peek()
            try:
                client = OpenMeteoClient(latitude=lat, longitude=lon)
                snapshot = await client.current()
                forecast = await client.forecast_hours(hours=forecast_hours)
            except Exception as e:
                log.warning("Weather refresh failed (%s); returning stale "
                             "cache (age=%s)", e, self.age_seconds())
                return self.peek()
            self._snapshot = snapshot
            self._forecast_hours = forecast
            self._pulled_at = datetime.utcnow()
            log.info("Weather cache refreshed (age now 0s, snapshot %s)",
                      snapshot.observed_at)
            state = self.peek()
            state = WeatherCacheState(
                snapshot=state.snapshot, pulled_at=state.pulled_at,
                age_seconds=state.age_seconds, fresh=state.fresh,
                refreshed_this_call=True,
                forecast_hours=state.forecast_hours,
                mode=self._mode, ttl_s=self.ttl_s,
            )
            return state

    def reset(self) -> None:
        """Wipe the cache. Useful in tests + after a site relocation."""
        self._snapshot = None
        self._pulled_at = None
        self._forecast_hours = None


_cache: WeatherCache | None = None


def get_weather_cache() -> WeatherCache:
    """Process-wide singleton accessor."""
    global _cache
    if _cache is None:
        _cache = WeatherCache()
    return _cache
