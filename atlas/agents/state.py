"""Shared in-memory state between Critic, Operator, and the HTTP layer.

The Critic periodically writes its latest weather assessment here. The
Operator reads that and writes back its verdict (GO / CAUTION / NO-GO).
API routes read both for the dashboard's Tonight + Weather tabs.

This is intentionally a tiny module — no DB persistence, no asyncio
primitives. The agents' message bus already covers the durable +
ordered case; this module just gives us a cheap, current-value cache
so a dashboard request doesn't have to wait for the next 5-minute
Critic tick to render something useful.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from threading import Lock
from typing import Any, Optional


# ---- Verdict levels ---------------------------------------------------------

VERDICT_GO = "GO"
VERDICT_CAUTION = "CAUTION"
VERDICT_NOGO = "NO-GO"
VERDICT_UNKNOWN = "UNKNOWN"


# ---- Assessment shape -------------------------------------------------------

@dataclass
class MetricCheck:
    """One per-metric check the Critic ran (wind, dew margin, cloud, ...)."""
    metric: str
    severity: str  # "ok" | "warning" | "critical"
    value: Optional[float]
    threshold: Optional[float]
    note: str


@dataclass
class WeatherAssessment:
    """The Critic's latest read on the sky. Fed to the Operator."""
    observed_at: str            # ISO timestamp from Open-Meteo
    assessed_at: str            # ISO timestamp when the Critic ran
    overall_severity: str       # "ok" | "warning" | "critical"
    summary: str                # one-line plain-English summary
    checks: list[MetricCheck] = field(default_factory=list)
    raw_current: dict = field(default_factory=dict)
    # Forward-looking: rough quality bucket for each of the next N hours
    # ("ok"/"warning"/"critical"), so the dashboard can shade the timeline.
    hourly_severity: list[dict] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


@dataclass
class OperatorVerdict:
    """Operator's call, derived from the Critic's assessment + any active
    alerts + session state. The Tonight tab banner reads this directly."""
    decided_at: str
    verdict: str                # GO | CAUTION | NO-GO | UNKNOWN
    reason: str                 # one-line plain-English
    sources: list[str] = field(default_factory=list)   # what fed the call

    def to_jsonable(self) -> dict:
        return asdict(self)


# ---- Singleton store --------------------------------------------------------

class _ObservatoryState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._assessment: WeatherAssessment | None = None
        self._verdict: OperatorVerdict | None = None

    # Critic writes here ----------------------------------------------------
    def set_assessment(self, a: WeatherAssessment) -> None:
        with self._lock:
            self._assessment = a

    def get_assessment(self) -> WeatherAssessment | None:
        with self._lock:
            return self._assessment

    # Operator writes here --------------------------------------------------
    def set_verdict(self, v: OperatorVerdict) -> OperatorVerdict | None:
        """Returns the previous verdict (or None) so callers can detect
        a change and broadcast accordingly."""
        with self._lock:
            prev = self._verdict
            self._verdict = v
            return prev

    def get_verdict(self) -> OperatorVerdict | None:
        with self._lock:
            return self._verdict


_state: _ObservatoryState | None = None


def get_state() -> _ObservatoryState:
    global _state
    if _state is None:
        _state = _ObservatoryState()
    return _state
