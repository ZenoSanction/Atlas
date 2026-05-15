"""Open-Meteo weather client. Free, no API key, good amateur-grade accuracy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from atlas.logging_setup import get_logger

log = get_logger("weather.openmeteo")


@dataclass
class WeatherSnapshot:
    temperature_c: float
    humidity_pct: float
    dew_point_c: float
    wind_speed_ms: float
    wind_gust_ms: Optional[float]
    cloud_cover_pct: float
    pressure_hpa: float
    precip_mm: float
    observed_at: str  # ISO timestamp from API


class OpenMeteoClient:
    BASE = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, latitude: float, longitude: float,
                 timeout: float = 10.0) -> None:
        self._lat = latitude
        self._lon = longitude
        self._timeout = timeout

    async def current(self) -> WeatherSnapshot:
        params = {
            "latitude": self._lat, "longitude": self._lon,
            "current": "temperature_2m,relative_humidity_2m,dew_point_2m,"
                       "wind_speed_10m,wind_gusts_10m,cloud_cover,"
                       "surface_pressure,precipitation",
            "timezone": "UTC",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(self.BASE, params=params)
            r.raise_for_status()
            data = r.json()["current"]
        return WeatherSnapshot(
            temperature_c=data["temperature_2m"],
            humidity_pct=data["relative_humidity_2m"],
            dew_point_c=data["dew_point_2m"],
            wind_speed_ms=data["wind_speed_10m"],
            wind_gust_ms=data.get("wind_gusts_10m"),
            cloud_cover_pct=data["cloud_cover"],
            pressure_hpa=data["surface_pressure"],
            precip_mm=data["precipitation"],
            observed_at=data["time"],
        )

    async def forecast_hours(self, hours: int = 12) -> list[dict]:
        params = {
            "latitude": self._lat, "longitude": self._lon,
            "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m,"
                      "wind_speed_10m,wind_gusts_10m,cloud_cover,precipitation",
            "forecast_hours": hours,
            "timezone": "UTC",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(self.BASE, params=params)
            r.raise_for_status()
            data = r.json()["hourly"]
        out = []
        for i, t in enumerate(data["time"]):
            out.append({
                "time": t,
                "temperature_c": data["temperature_2m"][i],
                "humidity_pct": data["relative_humidity_2m"][i],
                "dew_point_c": data["dew_point_2m"][i],
                "wind_speed_ms": data["wind_speed_10m"][i],
                "wind_gust_ms": data["wind_gusts_10m"][i],
                "cloud_cover_pct": data["cloud_cover"][i],
                "precip_mm": data["precipitation"][i],
            })
        return out
