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
    from atlas.units import c_to_f, c_delta_to_f, ms_to_mph, mm_to_in, hpa_to_inhg
    dm_c = snap.temperature_c - snap.dew_point_c
    return {
        "observed_at_utc": snap.observed_at,
        "temperature_f": round(c_to_f(snap.temperature_c), 1),
        "humidity_pct": round(snap.humidity_pct, 0),
        "dew_point_f": round(c_to_f(snap.dew_point_c), 1),
        "dew_margin_f": round(c_delta_to_f(dm_c), 1),
        "wind_speed_mph": round(ms_to_mph(snap.wind_speed_ms), 1),
        "wind_gust_mph": (round(ms_to_mph(snap.wind_gust_ms), 1)
                            if snap.wind_gust_ms is not None else None),
        "cloud_cover_pct": round(snap.cloud_cover_pct, 0),
        "pressure_inhg": round(hpa_to_inhg(snap.pressure_hpa), 2),
        "precip_in": round(mm_to_in(snap.precip_mm), 3),
        "site_lat": lat,
        "site_lon": lon,
        "units": "imperial",
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
    from atlas.units import c_to_f, c_delta_to_f, ms_to_mph, mm_to_in
    trimmed = []
    for r in rows:
        dm_c = r["temperature_c"] - r["dew_point_c"]
        trimmed.append({
            "time_utc": r["time"],
            "temperature_f": round(c_to_f(r["temperature_c"]), 1),
            "humidity_pct": round(r["humidity_pct"], 0),
            "dew_point_f": round(c_to_f(r["dew_point_c"]), 1),
            "dew_margin_f": round(c_delta_to_f(dm_c), 1),
            "wind_speed_mph": round(ms_to_mph(r["wind_speed_ms"]), 1),
            "wind_gust_mph": (round(ms_to_mph(r["wind_gust_ms"]), 1)
                                if r.get("wind_gust_ms") is not None else None),
            "cloud_cover_pct": round(r["cloud_cover_pct"], 0),
            "precip_in": round(mm_to_in(r["precip_mm"]), 3),
        })
    return {
        "hours_requested": hours,
        "site_lat": lat,
        "site_lon": lon,
        "hourly": trimmed,
        "units": "imperial",
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

async def _capture_test_frame(p: dict) -> dict:
    """Trigger a one-off NINA capture (bias / dark / light) and register
    the resulting FITS file in ATLAS's frames table.

    Useful for bench testing: with the camera connected but the scope
    capped, you can take a bias (0s) or dark (any exposure) and verify
    the full capture-and-ingest pipeline end-to-end."""
    exposure_s = float(p.get("exposure_s", 0.0))
    if exposure_s < 0 or exposure_s > 3600:
        return {"error": "exposure_s must be 0..3600"}
    frame_type = (p.get("frame_type") or "dark").lower().strip()
    if frame_type not in ("bias", "dark", "light"):
        return {"error": "frame_type must be bias / dark / light"}
    gain = p.get("gain")
    filter_name = p.get("filter_name")
    wait_s = float(p.get("wait_seconds_after", 5.0))

    from atlas.config import get_settings
    settings = get_settings()
    if settings.simulation_mode:
        return {"error": "Simulation mode is on — capture wouldn't produce a real "
                          "file. Turn off ATLAS_SIMULATION_MODE first."}

    from atlas.db.managers import ConfigManager
    equip = ConfigManager.get_equipment()
    if equip is None:
        return {"error": "Equipment profile not configured. Open Setup → Equipment."}

    try:
        from atlas.hardware.nina import NinaClient
        async with NinaClient(host=equip.nina_host, port=equip.nina_port,
                                timeout=10.0) as nina:
            result = await nina.camera_capture(
                exposure_s=exposure_s, gain=gain, filter_name=filter_name,
            )
    except Exception as e:
        return {"error": f"NINA capture call failed: {type(e).__name__}: {e}"}

    # NINA's Advanced API returns response data; the exact shape varies by
    # plugin version. Surface what we got so the operator can debug.
    out = {
        "ok": True,
        "exposure_s": exposure_s,
        "frame_type": frame_type,
        "filter_name": filter_name,
        "gain": gain,
        "nina_response": result,
        "wait_seconds_after": wait_s,
    }

    # Optional auto-ingest: if NINA tells us the file path in its response,
    # register it. Otherwise the operator must follow up with register_frame.
    saved_path = None
    if isinstance(result, dict):
        for key in ("file_path", "filename", "path", "ImagePath"):
            if key in result and result[key]:
                saved_path = result[key]
                break

    if saved_path:
        try:
            import asyncio
            await asyncio.sleep(wait_s)   # let NINA finish writing
            from atlas.capture.ingest import register_frame
            fid = register_frame(saved_path, frame_type=frame_type)
            out["frame_id"] = fid
            out["saved_path"] = saved_path
            out["message"] = (f"Captured {exposure_s}s {frame_type}, "
                                f"registered as frame #{fid}.")
        except Exception as e:
            out["ingest_warning"] = (f"Capture OK but auto-ingest failed: {e}. "
                                       "Use register_frame with the saved path.")
    else:
        out["message"] = ("Capture triggered. NINA didn't return a file path "
                            "in its response — follow up with register_frame once "
                            "the file lands on disk.")
    return out


CAPTURE_TOOL = ToolSpec(
    name="capture_test_frame",
    description=(
        "Take a single bench-test frame via NINA and register it in ATLAS. "
        "Designed for hardware-on-bench verification: with the scope "
        "capped you can still take bias (0s) or dark frames and verify "
        "the full capture → ingest → DB chain works. Returns the frame "
        "id on success. Use frame_type='bias' for 0s readouts, "
        "frame_type='dark' for any closed-shutter exposure, "
        "frame_type='light' if pointed at something real. Refuses to "
        "run in simulation mode."),
    input_schema={
        "type": "object",
        "properties": {
            "exposure_s": {"type": "number", "minimum": 0, "maximum": 3600,
                              "description": "Exposure time in seconds. 0 = bias."},
            "frame_type": {"type": "string",
                              "enum": ["bias", "dark", "light"],
                              "description": "What kind of frame. Defaults to dark."},
            "gain": {"type": "integer",
                       "description": "Camera gain. Optional; uses NINA default if omitted."},
            "filter_name": {"type": "string",
                               "description": "Filter to use. Omit for current filter."},
            "wait_seconds_after": {"type": "number",
                                       "description": "Seconds to wait after capture "
                                                       "for NINA to finish writing the "
                                                       "file (default 5)."},
        },
        "required": ["exposure_s"],
    },
    handler=_capture_test_frame,
)


def all_operator_tools() -> list[ToolSpec]:
    """Return the full set of tools the Operator should register."""
    return [*WEATHER_TOOLS, SYSTEM_STATUS_TOOL, CAPTURE_TOOL]
