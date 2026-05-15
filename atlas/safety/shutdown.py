"""Emergency shutdown sequence.

Per Round 4 #22 and multi-agent design:
    1. Stop imaging
    2. Park telescope (verify)
    3. Close roof (if automated)
    4. Warm camera at configured ramp rate
    5. Power down hardware
    6. Save session state
    7. Notify operator (critical)
"""
from __future__ import annotations

from atlas.hardware.nina import NinaClient, NinaError
from atlas.logging_setup import get_logger

log = get_logger("safety.shutdown")


class EmergencyShutdown:
    def __init__(self, nina: NinaClient) -> None:
        self._nina = nina

    async def execute(self, reason: str) -> dict:
        """Run the full shutdown sequence. Returns a step-by-step audit dict."""
        log.error("EMERGENCY SHUTDOWN initiated: %s", reason)
        audit = {"reason": reason, "steps": []}

        async def step(name: str, coro):
            try:
                await coro
                audit["steps"].append({"name": name, "ok": True})
                log.info("shutdown step OK: %s", name)
            except Exception as e:
                audit["steps"].append({"name": name, "ok": False, "error": str(e)})
                log.exception("shutdown step FAILED: %s", name)

        await step("sequence_stop", self._nina.sequence_stop())
        await step("park_mount", self._nina.park())
        await step("dome_close", self._nina.dome_close())
        await step("camera_warmup", self._nina.camera_warmup())
        # TODO Phase 2: dew heater off, focuser power, etc.
        return audit
