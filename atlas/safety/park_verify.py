"""Mount-park position verification.

After the safe-shutdown sequence parks the mount, we verify the
mount is actually where the operator told us it should be. Without
this check, a stalled park command leaves the mount tracking the
sky for hours — risking cable wrap, dew on optics held high, or
sun exposure when daylight arrives.

Flow:
    safe-shutdown -> park command -> wait briefly -> verify_park()
    -> if ok: emit park_verified
    -> if mismatch: emit park_mismatch + retry park
    -> after N retries: emit park_failed + page operator

The configured park position lives on EquipmentProfile.park_alt_deg /
park_az_deg / park_tolerance_deg. Tolerance defaults to 2 deg, which
covers most amateur GEM home positions (the published GoTo accuracy
is typically 0.5-1 deg, plus encoder rounding).
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Optional

from atlas.logging_setup import get_logger

log = get_logger("safety.park_verify")


# Defaults if EquipmentProfile values are missing
DEFAULT_TOLERANCE_DEG = 2.0
DEFAULT_RETRY_COUNT = 3
DEFAULT_SETTLE_S = 5.0


@dataclass
class ParkVerification:
    verified: bool
    reported_alt_deg: Optional[float]
    reported_az_deg: Optional[float]
    expected_alt_deg: float
    expected_az_deg: float
    delta_alt_deg: Optional[float]
    delta_az_deg: Optional[float]
    angular_offset_deg: Optional[float]
    tolerance_deg: float
    reason: str
    retries_used: int = 0

    def to_jsonable(self) -> dict:
        return {
            "verified": self.verified,
            "reported_alt_deg": self.reported_alt_deg,
            "reported_az_deg": self.reported_az_deg,
            "expected_alt_deg": self.expected_alt_deg,
            "expected_az_deg": self.expected_az_deg,
            "delta_alt_deg": self.delta_alt_deg,
            "delta_az_deg": self.delta_az_deg,
            "angular_offset_deg": self.angular_offset_deg,
            "tolerance_deg": self.tolerance_deg,
            "reason": self.reason,
            "retries_used": self.retries_used,
        }


def _angular_separation_deg(alt1, az1, alt2, az2) -> float:
    """Great-circle distance between two alt/az points (degrees)."""
    if any(v is None for v in (alt1, az1, alt2, az2)):
        return float("inf")
    a1 = math.radians(alt1); a2 = math.radians(alt2)
    A = math.radians(az1 - az2)
    cosd = (math.sin(a1) * math.sin(a2)
            + math.cos(a1) * math.cos(a2) * math.cos(A))
    cosd = max(-1.0, min(1.0, cosd))
    return math.degrees(math.acos(cosd))


def _extract_alt_az(info: dict) -> tuple[Optional[float], Optional[float]]:
    """Pull alt/az out of a telescope_info / mount_info dict, tolerating
    multiple key spellings (different mount drivers report differently).

    Cannot use ``or`` chains here — altitude=0.0 is a legitimate value
    (mount parked at the horizon) and Python treats 0.0 as falsy."""
    if not info:
        return None, None
    alt = info.get("altitude")
    if alt is None:
        alt = info.get("alt")
    if alt is None:
        alt = info.get("alt_deg")
    az = info.get("azimuth")
    if az is None:
        az = info.get("az")
    if az is None:
        az = info.get("az_deg")
    try:
        alt_f = float(alt) if alt is not None else None
        az_f = float(az) if az is not None else None
    except (TypeError, ValueError):
        return None, None
    return alt_f, az_f


async def verify_park(*, nina, expected_alt_deg: float,
                          expected_az_deg: float,
                          tolerance_deg: float = DEFAULT_TOLERANCE_DEG,
                          settle_s: float = DEFAULT_SETTLE_S,
                          ) -> ParkVerification:
    """Read the mount's reported alt/az and compare to expected.

    Brief settle delay (settle_s) before reading so a fresh park command
    has time to land. Returns a ParkVerification describing what was
    seen + whether it passed."""
    if settle_s > 0:
        await asyncio.sleep(settle_s)
    info: dict = {}
    try:
        info = await nina.telescope_info()
    except Exception as e:
        log.warning("telescope_info during park verify failed: %s", e)
        info = {}
    alt, az = _extract_alt_az(info)
    if alt is None or az is None:
        return ParkVerification(
            verified=False,
            reported_alt_deg=alt, reported_az_deg=az,
            expected_alt_deg=expected_alt_deg,
            expected_az_deg=expected_az_deg,
            delta_alt_deg=None, delta_az_deg=None,
            angular_offset_deg=None, tolerance_deg=tolerance_deg,
            reason=("mount reported no alt/az — driver may not expose "
                      "park position; manual verification needed"),
        )
    d_alt = alt - expected_alt_deg
    d_az = az - expected_az_deg
    # Normalize az delta into [-180, 180]
    while d_az > 180.0:
        d_az -= 360.0
    while d_az < -180.0:
        d_az += 360.0
    sep = _angular_separation_deg(alt, az,
                                       expected_alt_deg, expected_az_deg)
    ok = sep <= tolerance_deg
    return ParkVerification(
        verified=ok,
        reported_alt_deg=alt, reported_az_deg=az,
        expected_alt_deg=expected_alt_deg,
        expected_az_deg=expected_az_deg,
        delta_alt_deg=round(d_alt, 2), delta_az_deg=round(d_az, 2),
        angular_offset_deg=round(sep, 2),
        tolerance_deg=tolerance_deg,
        reason=(f"alt/az within tolerance ({sep:.2f}° <= {tolerance_deg:.1f}°)"
                  if ok else
                  f"alt/az off by {sep:.2f}° (> {tolerance_deg:.1f}° tolerance)"),
    )


async def park_and_verify(*, nina, expected_alt_deg: float,
                                expected_az_deg: float,
                                tolerance_deg: float = DEFAULT_TOLERANCE_DEG,
                                retries: int = DEFAULT_RETRY_COUNT,
                                settle_s: float = DEFAULT_SETTLE_S,
                                ) -> ParkVerification:
    """Park + verify with N retries.

    Each attempt: park command -> settle -> verify. If the verify
    fails, log + retry. Returns the final ParkVerification (the
    `retries_used` field tells the caller whether a retry was needed)."""
    last: Optional[ParkVerification] = None
    for attempt in range(max(1, int(retries))):
        try:
            await nina.park()
        except Exception as e:
            log.warning("park command failed on attempt %d: %s",
                          attempt + 1, e)
        result = await verify_park(
            nina=nina, expected_alt_deg=expected_alt_deg,
            expected_az_deg=expected_az_deg,
            tolerance_deg=tolerance_deg, settle_s=settle_s,
        )
        result.retries_used = attempt
        last = result
        if result.verified:
            return result
        log.warning("park verify attempt %d failed: %s",
                      attempt + 1, result.reason)
    # All retries exhausted
    if last is None:
        last = ParkVerification(
            verified=False,
            reported_alt_deg=None, reported_az_deg=None,
            expected_alt_deg=expected_alt_deg,
            expected_az_deg=expected_az_deg,
            delta_alt_deg=None, delta_az_deg=None,
            angular_offset_deg=None, tolerance_deg=tolerance_deg,
            reason="no verify attempts completed",
        )
    last.reason = (f"park failed after {retries} attempt(s) — "
                     + last.reason)
    return last


def load_park_target() -> tuple[float, float, float]:
    """Return (alt, az, tolerance) from the active EquipmentProfile."""
    from atlas.db.managers import ConfigManager
    eq = ConfigManager.get_equipment()
    if eq is None:
        return (0.0, 0.0, DEFAULT_TOLERANCE_DEG)
    return (
        float(getattr(eq, "park_alt_deg", 0.0) or 0.0),
        float(getattr(eq, "park_az_deg", 0.0) or 0.0),
        float(getattr(eq, "park_tolerance_deg",
                          DEFAULT_TOLERANCE_DEG) or DEFAULT_TOLERANCE_DEG),
    )
