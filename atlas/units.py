"""Unit conversion + Eastern-time helpers.

ATLAS keeps every numeric value in SI internally (so thresholds, astronomy,
and database storage stay consistent and reusable). Conversion to imperial
and to the operator's local time zone happens at the *edges*: API JSON
responses, agent chat tool outputs, alert messages, and the dashboard's
display layer.

Centralising the conversion here means there's exactly one place to change
the precision / rounding rules or to add a per-deploy units preference
later if a metric-loving observatory wants to switch back.
"""
from __future__ import annotations

from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — Python without tzdata
    EASTERN = timezone.utc  # graceful fallback


# ---- Temperature ----------------------------------------------------------

def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def c_delta_to_f(dc: float) -> float:
    """Convert a temperature *delta* (e.g. dew margin) C to F.
    A 1 °C delta is 1.8 °F (no +32 offset)."""
    return dc * 9.0 / 5.0


def f_delta_to_c(df: float) -> float:
    return df * 5.0 / 9.0


# ---- Speed / wind ---------------------------------------------------------

_MS_PER_MPH = 0.44704  # exact


def ms_to_mph(v: float) -> float:
    return v / _MS_PER_MPH


def mph_to_ms(v: float) -> float:
    return v * _MS_PER_MPH


# ---- Precipitation --------------------------------------------------------

def mm_to_in(v: float) -> float:
    return v / 25.4


def in_to_mm(v: float) -> float:
    return v * 25.4


# ---- Pressure -------------------------------------------------------------

def hpa_to_inhg(v: float) -> float:
    return v * 0.02953


def inhg_to_hpa(v: float) -> float:
    return v / 0.02953


# ---- Distance / elevation -------------------------------------------------

def m_to_ft(v: float) -> float:
    return v * 3.28084


def ft_to_m(v: float) -> float:
    return v / 3.28084


# ---- Pretty-printers ------------------------------------------------------

def fmt_f(c: float, decimals: int = 1) -> str:
    return f"{c_to_f(c):.{decimals}f}°F"


def fmt_f_delta(dc: float, decimals: int = 1) -> str:
    return f"{c_delta_to_f(dc):.{decimals}f}°F"


def fmt_mph(ms: float, decimals: int = 1) -> str:
    return f"{ms_to_mph(ms):.{decimals}f} mph"


def fmt_in(mm: float, decimals: int = 2) -> str:
    return f"{mm_to_in(mm):.{decimals}f} in"


def fmt_inhg(hpa: float, decimals: int = 2) -> str:
    return f"{hpa_to_inhg(hpa):.{decimals}f} inHg"


def fmt_ft(m: float, decimals: int = 0) -> str:
    return f"{m_to_ft(m):.{decimals}f} ft"


# ---- Time -----------------------------------------------------------------

def now_eastern() -> datetime:
    return datetime.now(EASTERN)


def to_eastern(when_utc: datetime) -> datetime:
    """Convert a naive UTC datetime (or a UTC-aware one) to America/New_York,
    which handles EST/EDT transitions correctly."""
    if when_utc.tzinfo is None:
        when_utc = when_utc.replace(tzinfo=timezone.utc)
    return when_utc.astimezone(EASTERN)


def fmt_eastern(when_utc: datetime, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    return to_eastern(when_utc).strftime(fmt)


def fmt_eastern_short(when_utc: datetime) -> str:
    return to_eastern(when_utc).strftime("%H:%M %Z")


# ---- Convenience: convert a whole snapshot dict to imperial ---------------

def snapshot_to_imperial(snap: dict) -> dict:
    """Take a metric weather snapshot dict (Open-Meteo style keys) and
    return the same shape with imperial values + renamed keys."""
    out = dict(snap)  # keep raw
    if "temperature_c" in snap:
        out["temperature_f"] = round(c_to_f(snap["temperature_c"]), 1)
    if "dew_point_c" in snap:
        out["dew_point_f"] = round(c_to_f(snap["dew_point_c"]), 1)
    if "dew_margin_c" in snap:
        out["dew_margin_f"] = round(c_delta_to_f(snap["dew_margin_c"]), 1)
    if "wind_speed_ms" in snap:
        out["wind_speed_mph"] = round(ms_to_mph(snap["wind_speed_ms"]), 1)
    if "wind_gust_ms" in snap and snap["wind_gust_ms"] is not None:
        out["wind_gust_mph"] = round(ms_to_mph(snap["wind_gust_ms"]), 1)
    elif "wind_gust_ms" in snap:
        out["wind_gust_mph"] = None
    if "pressure_hpa" in snap:
        out["pressure_inhg"] = round(hpa_to_inhg(snap["pressure_hpa"]), 2)
    if "precip_mm" in snap:
        out["precip_in"] = round(mm_to_in(snap["precip_mm"]), 3)
    return out
