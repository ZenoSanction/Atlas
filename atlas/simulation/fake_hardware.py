"""Fake NINA + PHD2 implementations for simulation mode."""
from __future__ import annotations

import asyncio
from typing import Any


class FakeNina:
    """API-compatible stand-in for NinaClient that always succeeds."""

    def __init__(self, *args, **kwargs) -> None:
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def ping(self) -> bool:
        return True

    async def camera_info(self) -> dict:
        return {"connected": True, "name": "Simulated Camera",
                 "temperature": -10.0, "cooling": True}

    async def telescope_info(self) -> dict:
        return {"connected": True, "name": "Simulated Telescope",
                 "ra": 0.0, "dec": 0.0, "tracking": True, "parked": False}

    async def focuser_info(self) -> dict:
        return {"connected": True, "position": 15000, "max_position": 30000}

    async def filterwheel_info(self) -> dict:
        return {"connected": True, "filters": ["L","R","G","B","V","Ha"],
                 "current_filter": "L"}

    async def mount_info(self) -> dict:
        return {"connected": True, "parked": True}

    async def dome_info(self) -> dict:
        return {"connected": True, "open": False}

    async def focuser_move(self, position: int) -> Any:
        await asyncio.sleep(0.1)
        return {"ok": True, "position": position}

    async def camera_capture(self, exposure_s: float, **k) -> Any:
        await asyncio.sleep(min(exposure_s / 60, 0.3))
        return {"ok": True, "frame_id": "sim-frame", "exposure_s": exposure_s}

    async def camera_set_cooling(self, target_c: float) -> Any:
        return {"ok": True, "target_c": target_c}

    async def camera_warmup(self) -> Any:
        return {"ok": True}

    async def slew(self, ra_hours: float, dec_deg: float) -> Any:
        await asyncio.sleep(0.2)
        return {"ok": True, "ra": ra_hours, "dec": dec_deg}

    async def park(self) -> Any:
        return {"ok": True, "parked": True}

    async def unpark(self) -> Any:
        return {"ok": True, "parked": False}

    async def dome_open(self) -> Any:
        return {"ok": True, "open": True}

    async def dome_close(self) -> Any:
        return {"ok": True, "open": False}

    async def sequence_start(self, spec: dict) -> Any:
        return {"ok": True, "running": True}

    async def sequence_stop(self) -> Any:
        return {"ok": True, "running": False}

    async def sequence_status(self) -> Any:
        return {"running": False}


class FakePhd2:
    """API-compatible stand-in for Phd2Client."""

    def __init__(self, *args, **kwargs) -> None:
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc_info):
        await self.close()

    async def call(self, method: str, params: list | None = None) -> Any:
        return {"ok": True, "method": method}

    async def ping(self) -> bool:
        return True

    async def get_app_state(self) -> str:
        return "Guiding"

    async def get_pixel_scale(self) -> float:
        return 2.5

    async def get_calibrated(self) -> bool:
        return True

    async def guide(self, **k) -> Any:
        return {"ok": True}

    async def stop_capture(self) -> Any:
        return {"ok": True}

    async def dither(self, **k) -> Any:
        return {"ok": True}
