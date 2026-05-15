"""PHD2 JSON-RPC client.

PHD2 listens on TCP port 4400 by default. The protocol is newline-delimited
JSON-RPC 2.0. We expose a small high-level interface around guiding-state and
guiding-stats which is what the Critic agent polls.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from atlas.logging_setup import get_logger

log = get_logger("hardware.phd2")


class Phd2Error(RuntimeError):
    pass


class Phd2Client:
    def __init__(self, host: str = "localhost", port: int = 4400,
                 timeout: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._req_id = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        if self._writer is not None:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout,
            )
        except (OSError, asyncio.TimeoutError) as e:
            raise Phd2Error(f"PHD2 connect failed: {e}") from e

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def __aenter__(self) -> "Phd2Client":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def call(self, method: str, params: list | None = None) -> Any:
        await self.connect()
        self._req_id += 1
        req = {"method": method, "id": self._req_id}
        if params is not None:
            req["params"] = params
        self._writer.write((json.dumps(req) + "\r\n").encode("utf-8"))
        await self._writer.drain()

        # Read until we find the response with our id (PHD2 also sends events)
        while True:
            try:
                line = await asyncio.wait_for(self._reader.readline(),
                                                timeout=self._timeout)
            except asyncio.TimeoutError:
                raise Phd2Error(f"PHD2 timeout waiting for {method}")
            if not line:
                raise Phd2Error("PHD2 closed the connection")
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except ValueError:
                continue
            if msg.get("id") == self._req_id:
                if "error" in msg:
                    raise Phd2Error(f"PHD2 error: {msg['error']}")
                return msg.get("result")
            # Skip event messages

    # --- convenience --------------------------------------------------------

    async def ping(self) -> bool:
        try:
            await self.call("get_app_state")
            return True
        except Phd2Error:
            return False

    async def get_app_state(self) -> str:
        return await self.call("get_app_state")

    async def get_pixel_scale(self) -> float:
        return await self.call("get_pixel_scale")

    async def get_calibrated(self) -> bool:
        return await self.call("get_calibrated")

    async def guide(self, settle_pixels: float = 1.5, settle_time_s: int = 10,
                    settle_timeout_s: int = 60) -> Any:
        return await self.call("guide", [{
            "pixels": settle_pixels, "time": settle_time_s,
            "timeout": settle_timeout_s,
        }, False])

    async def stop_capture(self) -> Any:
        return await self.call("stop_capture")

    async def dither(self, amount_px: float = 5.0,
                      settle_pixels: float = 1.5,
                      settle_time_s: int = 10,
                      settle_timeout_s: int = 60) -> Any:
        return await self.call("dither", [amount_px, False, {
            "pixels": settle_pixels, "time": settle_time_s,
            "timeout": settle_timeout_s,
        }])
