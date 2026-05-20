"""Tools the Critic agent can use when chatted with.

The Critic is the watchdog. It reports sensor + sky state, never decides.
Tools below let it answer "what's the current weather?", "what's the
dew margin?", "what alerts are open?", "what thresholds are you using?"
"""
from __future__ import annotations

from datetime import datetime

from atlas.agents.base import ToolSpec
from atlas.agents.state import get_state
from atlas.db.managers import AlertManager, ConfigManager
from atlas.safety.thresholds import SafetyThresholds
from atlas.weather.openmeteo import OpenMeteoClient


async def _get_assessment(_p: dict) -> dict:
    a = get_state().get_assessment()
    if a is None:
        return {"assessment": None,
                "message": "No weather assessment computed yet."}
    return {"assessment": a.to_jsonable()}


async def _get_thresholds(_p: dict) -> dict:
    t = SafetyThresholds.from_db()
    return {
        "wind_speed_warn_ms": t.wind_speed_warn_ms,
        "wind_speed_critical_ms": t.wind_speed_critical_ms,
        "humidity_warn_pct": t.humidity_warn_pct,
        "humidity_critical_pct": t.humidity_critical_pct,
        "dew_margin_warn_c": t.dew_margin_warn_c,
        "dew_margin_critical_c": t.dew_margin_critical_c,
        "cloud_cover_warn_pct": t.cloud_cover_warn_pct,
        "cloud_cover_critical_pct": t.cloud_cover_critical_pct,
        "note": "Editable in the Setup tab. Critic re-reads these every 5 minutes.",
    }


async def _get_open_alerts(_p: dict) -> dict:
    alerts = AlertManager.unresolved()
    return {
        "count": len(alerts),
        "alerts": [
            {"id": a.id,
              "severity": a.severity.value if hasattr(a.severity, "value") else a.severity,
              "code": a.code, "message": a.message,
              "raised_at": a.raised_at.isoformat()}
            for a in alerts
        ],
    }


async def _quick_weather(_p: dict) -> dict:
    site = ConfigManager.get_site()
    if site is None:
        return {"error": "Site coordinates not configured."}
    try:
        c = OpenMeteoClient(float(site.latitude), float(site.longitude))
        snap = await c.current()
    except Exception as e:
        return {"error": f"Open-Meteo failed: {e}"}
    dm = snap.temperature_c - snap.dew_point_c
    return {
        "observed_at_utc": snap.observed_at,
        "temperature_c": round(snap.temperature_c, 1),
        "humidity_pct": round(snap.humidity_pct, 0),
        "dew_point_c": round(snap.dew_point_c, 1),
        "dew_margin_c": round(dm, 1),
        "wind_speed_ms": round(snap.wind_speed_ms, 1),
        "wind_gust_ms": (round(snap.wind_gust_ms, 1)
                          if snap.wind_gust_ms is not None else None),
        "cloud_cover_pct": round(snap.cloud_cover_pct, 0),
        "precip_mm": round(snap.precip_mm, 2),
    }


CRITIC_TOOLS: list[ToolSpec] = [
    ToolSpec("get_current_assessment",
             "Get the Critic's most recent per-metric weather assessment "
             "(wind, dew margin, humidity, cloud cover, precip) with "
             "severity for each. This is what feeds the Operator's verdict.",
             {"type": "object", "properties": {}},
             _get_assessment),
    ToolSpec("get_thresholds",
             "Return the user-editable safety thresholds the Critic is "
             "applying (warn + critical) for each weather metric.",
             {"type": "object", "properties": {}},
             _get_thresholds),
    ToolSpec("get_open_alerts",
             "Return all currently unresolved alerts in the system (any agent).",
             {"type": "object", "properties": {}},
             _get_open_alerts),
    ToolSpec("get_current_weather",
             "Fetch a fresh Open-Meteo snapshot for the configured site "
             "right now (bypassing the 5-min Critic cycle).",
             {"type": "object", "properties": {}},
             _quick_weather),
]
