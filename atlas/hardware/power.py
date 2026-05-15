"""Power source monitoring.

Per Round 4 #22: ATLAS detects active power source via the OS UPS interface.
A backyard observatory may have multiple sources (solar / battery / utility /
generator). On Windows we use ``psutil.sensors_battery()`` plus optional
hooks for user-supplied source-discrimination.

The default trigger: shutdown at 50% battery OR <5 minutes runtime
remaining, whichever fires first. Tunable in Setup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import psutil

from atlas.logging_setup import get_logger

log = get_logger("hardware.power")


@dataclass
class PowerState:
    on_battery: bool
    battery_percent: Optional[float]
    seconds_left: Optional[int]
    plugged: Optional[bool]
    source: str  # "ac" | "battery" | "unknown"

    @property
    def runtime_minutes(self) -> Optional[float]:
        if self.seconds_left is None or self.seconds_left < 0:
            return None
        return self.seconds_left / 60.0


@dataclass
class PowerThresholds:
    """Defaults per Round 4 #22."""
    shutdown_battery_pct: float = 50.0
    shutdown_runtime_minutes: float = 5.0


class PowerMonitor:
    def __init__(self, thresholds: PowerThresholds | None = None) -> None:
        self._thresholds = thresholds or PowerThresholds()

    def read(self) -> PowerState:
        b = psutil.sensors_battery()
        if b is None:
            return PowerState(on_battery=False, battery_percent=None,
                              seconds_left=None, plugged=None, source="ac")
        return PowerState(
            on_battery=not b.power_plugged,
            battery_percent=b.percent,
            seconds_left=b.secsleft if b.secsleft != psutil.POWER_TIME_UNLIMITED else None,
            plugged=b.power_plugged,
            source="ac" if b.power_plugged else "battery",
        )

    def should_shutdown(self, state: PowerState | None = None) -> tuple[bool, str]:
        """Return (should_shutdown, reason)."""
        s = state or self.read()
        if not s.on_battery:
            return False, "on AC power"
        if s.battery_percent is not None and s.battery_percent < self._thresholds.shutdown_battery_pct:
            return True, f"battery {s.battery_percent:.0f}% < {self._thresholds.shutdown_battery_pct:.0f}% threshold"
        if s.runtime_minutes is not None and s.runtime_minutes < self._thresholds.shutdown_runtime_minutes:
            return True, f"runtime {s.runtime_minutes:.1f} min < {self._thresholds.shutdown_runtime_minutes:.1f} min threshold"
        return False, "on battery, within thresholds"
