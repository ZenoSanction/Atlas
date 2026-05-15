"""NINA Advanced API client.

NINA is the abstraction layer for every piece of hardware: telescope, camera,
focuser, filter wheel, dome/roof, dew heater, switch outputs. ATLAS does not
talk to ASCOM/INDI directly — if NINA supports it, ATLAS supports it.

API base: http://{host}:{port}/v2/api

Confirmed working endpoints (from prior build's diagnostics):
    /v2/api/equipment/camera/info
    /v2/api/equipment/telescope/info
    /v2/api/equipment/focuser/info
    /v2/api/equipment/filterwheel/info
    /v2/api/equipment/mount/...
    /v2/api/sequence/...
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from atlas.logging_setup import get_logger

log = get_logger("hardware.nina")


class NinaError(RuntimeError):
    """Raised when NINA returns a non-success status or is unreachable."""


class NinaClient:
    def __init__(self, host: str = "localhost", port: int = 1888,
                 timeout: float = 10.0) -> None:
        self._base = f"http://{host}:{port}/v2/api"
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "NinaClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base,
                                              timeout=self._timeout)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- low-level HTTP -----------------------------------------------------

    async def _get(self, path: str, **params) -> Any:
        await self.connect()
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise NinaError(f"NINA HTTP error: {e}") from e
        if r.status_code >= 400:
            raise NinaError(f"NINA {path} -> HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            return r.text or {}

    async def _post(self, path: str, json: dict | None = None, **params) -> Any:
        await self.connect()
        try:
            r = await self._client.post(path, json=json or {}, params=params)
        except httpx.HTTPError as e:
            raise NinaError(f"NINA HTTP error: {e}") from e
        if r.status_code >= 400:
            raise NinaError(f"NINA {path} -> HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            return r.text or {}

    # --- equipment status ---------------------------------------------------

    async def ping(self) -> bool:
        """True if NINA's API is reachable."""
        try:
            await self._get("/equipment/camera/info")
            return True
        except NinaError:
            return False

    async def camera_info(self) -> dict:
        return await self._get("/equipment/camera/info")

    async def telescope_info(self) -> dict:
        return await self._get("/equipment/telescope/info")

    async def focuser_info(self) -> dict:
        return await self._get("/equipment/focuser/info")

    async def filterwheel_info(self) -> dict:
        return await self._get("/equipment/filterwheel/info")

    async def mount_info(self) -> dict:
        return await self._get("/equipment/mount/info")

    async def dome_info(self) -> dict:
        return await self._get("/equipment/dome/info")

    # --- focuser ------------------------------------------------------------

    async def focuser_move(self, position: int) -> Any:
        """Note: NINA Advanced API v2 uses GET for focuser move (per prior
        build's investigation logs)."""
        return await self._get("/equipment/focuser/move", position=position)

    # --- camera -------------------------------------------------------------

    async def camera_capture(self, exposure_s: float, gain: int | None = None,
                              filter_name: str | None = None) -> Any:
        params = {"exposure": exposure_s}
        if gain is not None:
            params["gain"] = gain
        if filter_name is not None:
            params["filter"] = filter_name
        return await self._post("/equipment/camera/capture", params=params)

    async def camera_set_cooling(self, target_c: float) -> Any:
        return await self._get("/equipment/camera/cool", temperature=target_c)

    async def camera_warmup(self) -> Any:
        return await self._get("/equipment/camera/warm")

    # --- mount --------------------------------------------------------------

    async def slew(self, ra_hours: float, dec_deg: float) -> Any:
        return await self._post("/equipment/mount/slew",
                                 json={"ra": ra_hours, "dec": dec_deg})

    async def park(self) -> Any:
        return await self._post("/equipment/mount/park")

    async def unpark(self) -> Any:
        return await self._post("/equipment/mount/unpark")

    # --- dome / roof --------------------------------------------------------

    async def dome_open(self) -> Any:
        return await self._post("/equipment/dome/open")

    async def dome_close(self) -> Any:
        return await self._post("/equipment/dome/close")

    # --- sequences ----------------------------------------------------------

    async def sequence_start(self, spec: dict) -> Any:
        return await self._post("/sequence/start", json=spec)

    async def sequence_stop(self) -> Any:
        return await self._post("/sequence/stop")

    async def sequence_status(self) -> Any:
        return await self._get("/sequence/status")
