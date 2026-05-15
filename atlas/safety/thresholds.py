"""Weather / environment threshold evaluation."""
from __future__ import annotations

from dataclasses import dataclass

from atlas.weather.openmeteo import WeatherSnapshot


@dataclass
class SafetyThresholds:
    wind_speed_warn_ms: float = 6.7        # ~15 mph
    wind_speed_critical_ms: float = 8.9    # ~20 mph
    humidity_warn_pct: float = 85.0
    humidity_critical_pct: float = 95.0
    dew_margin_warn_c: float = 5.0
    dew_margin_critical_c: float = 2.0
    cloud_cover_warn_pct: float = 60.0
    cloud_cover_critical_pct: float = 85.0


@dataclass
class ThresholdResult:
    severity: str   # "ok" | "warning" | "critical"
    code: str | None
    message: str | None
    data: dict


def evaluate_safety(snap: WeatherSnapshot,
                     thresholds: SafetyThresholds | None = None) -> ThresholdResult:
    """Return the highest-severity threshold breach for the snapshot, if any."""
    t = thresholds or SafetyThresholds()

    # Wind
    if snap.wind_speed_ms >= t.wind_speed_critical_ms:
        return ThresholdResult("critical", "wind_high",
            f"Wind {snap.wind_speed_ms:.1f} m/s ≥ critical {t.wind_speed_critical_ms:.1f}",
            {"wind_speed_ms": snap.wind_speed_ms})
    if snap.wind_speed_ms >= t.wind_speed_warn_ms:
        return ThresholdResult("warning", "wind_high",
            f"Wind {snap.wind_speed_ms:.1f} m/s ≥ warn {t.wind_speed_warn_ms:.1f}",
            {"wind_speed_ms": snap.wind_speed_ms})

    # Humidity
    if snap.humidity_pct >= t.humidity_critical_pct:
        return ThresholdResult("critical", "humidity_high",
            f"Humidity {snap.humidity_pct:.0f}% ≥ critical {t.humidity_critical_pct:.0f}%",
            {"humidity_pct": snap.humidity_pct})
    if snap.humidity_pct >= t.humidity_warn_pct:
        return ThresholdResult("warning", "humidity_high",
            f"Humidity {snap.humidity_pct:.0f}% ≥ warn {t.humidity_warn_pct:.0f}%",
            {"humidity_pct": snap.humidity_pct})

    # Dew margin
    dew_margin = snap.temperature_c - snap.dew_point_c
    if dew_margin <= t.dew_margin_critical_c:
        return ThresholdResult("critical", "dew_risk",
            f"Dew margin {dew_margin:.1f}°C ≤ critical {t.dew_margin_critical_c:.1f}",
            {"dew_margin_c": dew_margin})
    if dew_margin <= t.dew_margin_warn_c:
        return ThresholdResult("warning", "dew_risk",
            f"Dew margin {dew_margin:.1f}°C ≤ warn {t.dew_margin_warn_c:.1f}",
            {"dew_margin_c": dew_margin})

    # Cloud
    if snap.cloud_cover_pct >= t.cloud_cover_critical_pct:
        return ThresholdResult("critical", "cloud_cover",
            f"Cloud {snap.cloud_cover_pct:.0f}% ≥ critical {t.cloud_cover_critical_pct:.0f}%",
            {"cloud_cover_pct": snap.cloud_cover_pct})
    if snap.cloud_cover_pct >= t.cloud_cover_warn_pct:
        return ThresholdResult("warning", "cloud_cover",
            f"Cloud {snap.cloud_cover_pct:.0f}% ≥ warn {t.cloud_cover_warn_pct:.0f}%",
            {"cloud_cover_pct": snap.cloud_cover_pct})

    return ThresholdResult("ok", None, None, {})
