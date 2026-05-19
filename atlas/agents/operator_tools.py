"""Tools the Operator agent can call from a chat-style conversation.

These let ATLAS answer live questions from the dashboard's ATLAS tab
(text + voice) instead of guessing from training. The tools are wired
into the Operator's ``_tools`` registry in ``Operator.__init__`` so
``BaseAgent.think()`` advertises them on every Anthropic call.

All handlers are defensive: if the data source is unavailable, they
return a structured ``{"error": ...}`` payload rather than raising,
so a failure of one subsystem (e.g. NINA not yet running) does not
prevent the chat call from completing.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from atlas.agents.base import ToolSpec
from atlas.config import get_settings
from atlas.db.managers import ConfigManager
from atlas.weather.openmeteo import OpenMeteoClient


# ---- weather ----------------------------------------------------------------

def _site_coords() -> tuple[float, float] | None:
    """Return (lat, lon) from site_config, or None if not configured yet."""
    site = ConfigManager.get_site()
    if site is None:
        return None
    return float(site.latitude), float(site.longitude)


async def _get_current_weather(_params: dict) -> dict:
    coords = _site_coords()
    if coords is None:
        return {"error": "Site coordinates are not configured. Set "
                          "latitude/longitude in the Setup tab first."}
    lat, lon = coords
    try:
        client = OpenMeteoClient(latitude=lat, longitude=lon)
        snap = await client.current()
    except Exception as e:  # network, API, etc.
        return {"error": f"Open-Meteo request failed: {e}"}
    dew_margin = snap.temperature_c - snap.dew_point_c
    return {
        "observed_at_utc": snap.observed_at,
        "temperature_c": round(snap.temperature_c, 1),
        "humidity_pct": round(snap.humidity_pct, 0),
        "dew_point_c": round(snap.dew_point_c, 1),
        "dew_margin_c": round(dew_margin, 1),
        "wind_speed_ms": round(snap.wind_speed_ms, 1),
        "wind_gust_ms": (round(snap.wind_gust_ms, 1)
                         if snap.wind_gust_ms is not None else None),
        "cloud_cover_pct": round(snap.cloud_cover_pct, 0),
        "pressure_hpa": round(snap.pressure_hpa, 1),
        "precip_mm": round(snap.precip_mm, 2),
        "site_lat": lat,
        "site_lon": lon,
    }


async def _get_forecast(params: dict) -> dict:
    hours = int(params.get("hours", 12))
    hours = max(1, min(48, hours))
    coords = _site_coords()
    if coords is None:
        return {"error": "Site coordinates are not configured. Set "
                          "latitude/longitude in the Setup tab first."}
    lat, lon = coords
    try:
        client = OpenMeteoClient(latitude=lat, longitude=lon)
        rows = await client.forecast_hours(hours=hours)
    except Exception as e:
        return {"error": f"Open-Meteo request failed: {e}"}
    # Trim each row to the keys we care about + add dew margin.
    trimmed = []
    for r in rows:
        trimmed.append({
            "time_utc": r["time"],
            "temperature_c": round(r["temperature_c"], 1),
            "humidity_pct": round(r["humidity_pct"], 0),
            "dew_point_c": round(r["dew_point_c"], 1),
            "dew_margin_c": round(r["temperature_c"] - r["dew_point_c"], 1),
            "wind_speed_ms": round(r["wind_speed_ms"], 1),
            "wind_gust_ms": (round(r["wind_gust_ms"], 1)
                              if r.get("wind_gust_ms") is not None else None),
            "cloud_cover_pct": round(r["cloud_cover_pct"], 0),
            "precip_mm": round(r["precip_mm"], 2),
        })
    return {
        "hours_requested": hours,
        "site_lat": lat,
        "site_lon": lon,
        "hourly": trimmed,
    }


WEATHER_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="get_current_weather",
        description=(
            "Get the current weather at the observatory site from Open-Meteo. "
            "Returns temperature, humidity, dew point, dew margin (temperature "
            "minus dew point — the critical number for optics; below 2 C the "
            "Critic raises an alert), wind speed and gust, cloud cover, "
            "pressure, and precipitation. Use this for 'is it clear right now?' "
            "or 'what's the dew margin?' style questions."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_get_current_weather,
    ),
    ToolSpec(
        name="get_forecast",
        description=(
            "Get the hourly weather forecast for the observatory site from "
            "Open-Meteo. Default and recommended for 'tonight' questions is "
            "12 hours. Returns a list of hourly snapshots with temperature, "
            "humidity, dew point, dew margin, wind, cloud cover, and "
            "precipitation. Each row is timestamped in UTC."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Number of hours to forecast (1-48). "
                                    "Default 12. Use 12 for 'tonight', "
                                    "24 for 'next 24 hours', etc.",
                    "minimum": 1,
                    "maximum": 48,
                },
            },
        },
        handler=_get_forecast,
    ),
]


# ---- system status ----------------------------------------------------------

async def _ping_nina(host: str, port: int, timeout: float = 3.0) -> bool:
    """Best-effort TCP-level reachability for NINA's API."""
    try:
        from atlas.hardware.nina import NinaClient
        c = NinaClient(host=host, port=port, timeout=timeout)
        try:
            return await asyncio.wait_for(c.ping(), timeout=timeout + 1)
        finally:
            await c.close()
    except Exception:
        return False


async def _ping_phd2(host: str, port: int, timeout: float = 3.0) -> bool:
    """Best-effort TCP reachability for PHD2's JSON-RPC port."""
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _disk_free_gb(path: Path) -> float | None:
    try:
        _total, _used, free = shutil.disk_usage(str(path))
        return round(free / (1024 ** 3), 2)
    except Exception:
        return None


async def _get_system_status(_params: dict) -> dict:
    settings = get_settings()

    # Vault
    try:
        from atlas.security import get_vault
        vault = get_vault()
        vault_status = {
            "initialised": vault.is_initialised,
            "unlocked": vault.is_unlocked,
        }
    except Exception as e:
        vault_status = {"error": str(e)}

    # Agents
    try:
        from atlas.agents.coordinator import get_coordinator
        agents = get_coordinator().status()
    except Exception as e:
        agents = {"error": str(e)}

    # Config completeness
    site = ConfigManager.get_site()
    equipment = ConfigManager.get_equipment()
    site_configured = site is not None
    equipment_configured = equipment is not None

    # Hardware reachability (skip in simulation mode — it's not meaningful)
    nina_reachable: bool | None = None
    phd2_reachable: bool | None = None
    if settings.simulation_mode:
        nina_reachable = None
        phd2_reachable = None
    elif equipment is not None:
        nina_reachable = await _ping_nina(equipment.nina_host, equipment.nina_port)
        phd2_reachable = await _ping_phd2(equipment.phd2_host, equipment.phd2_port)

    return {
        "simulation_mode": settings.simulation_mode,
        "vault": vault_status,
        "agents": agents,
        "site_configured": site_configured,
        "equipment_configured": equipment_configured,
        "nina_reachable": nina_reachable,
        "phd2_reachable": phd2_reachable,
        "disk_free_gb_data": _disk_free_gb(settings.data_dir),
        "claude_model": settings.claude_model,
    }


SYSTEM_STATUS_TOOL = ToolSpec(
    name="get_system_status",
    description=(
        "Get a live snapshot of ATLAS system state: simulation mode flag, "
        "credential-vault initialisation/unlock state, status of each of the "
        "five agents (running, in safe-autonomous mode), whether site config "
        "and equipment profile are populated, NINA + PHD2 reachability (only "
        "meaningful when not in simulation mode), free disk space at the data "
        "directory, and the active Claude model. Use this for 'is everything "
        "online?', 'why is the GO/NO-GO red?', or 'is the vault locked?' "
        "style questions."
    ),
    input_schema={"type": "object", "properties": {}},
    handler=_get_system_status,
)


# ---- public API -------------------------------------------------------------

def all_operator_tools() -> list[ToolSpec]:
    """Return the full set of tools the Operator should register."""
    return [*WEATHER_TOOLS, SYSTEM_STATUS_TOOL]
