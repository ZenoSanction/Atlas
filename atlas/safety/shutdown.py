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
        # Park + verify in one operation. Verification math compares
        # the mount's reported alt/az against the configured safe
        # position; mismatch triggers retries up to N times, then
        # surfaces in the audit + escalates via notification dispatcher
        # at the Operator layer.
        from atlas.safety.park_verify import (
            load_park_target, park_and_verify,
        )
        alt, az, tol = load_park_target()
        verify = await park_and_verify(
            nina=self._nina, expected_alt_deg=alt,
            expected_az_deg=az, tolerance_deg=tol,
        )
        audit["steps"].append({
            "name": "park_mount",
            "ok": verify.verified,
            "verify": verify.to_jsonable(),
            "error": (None if verify.verified else verify.reason),
        })
        if verify.verified:
            log.info("shutdown step OK: park_mount (verified at "
                       "alt=%.2f az=%.2f, offset %.2f deg)",
                       verify.reported_alt_deg, verify.reported_az_deg,
                       verify.angular_offset_deg or 0.0)
        else:
            log.error("shutdown step FAILED: park_mount — %s",
                        verify.reason)
        await step("dome_close", self._nina.dome_close())
        await step("camera_warmup", self._nina.camera_warmup())
        # TODO Phase 2: dew heater off, focuser power, etc.
        return audit
