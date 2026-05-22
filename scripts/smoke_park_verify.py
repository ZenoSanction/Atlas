"""Smoke test for mount-park position verification.

Tests:
  1. _angular_separation_deg basic geometry.
  2. _extract_alt_az tolerates spelling variants.
  3. verify_park happy path: FakeNina parks to configured position.
  4. verify_park mismatch: FakeNina parks somewhere else.
  5. verify_park no-alt-az: mount doesn't report alt/az -> manual flag.
  6. park_and_verify retries on mismatch.
  7. EmergencyShutdown emits verify dict in its audit.

Run from project root:
    venv\\Scripts\\python.exe scripts\\smoke_park_verify.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def test_angular_separation() -> None:
    _hr("1. _angular_separation_deg basic geometry")
    from atlas.safety.park_verify import _angular_separation_deg
    # Same point
    assert abs(_angular_separation_deg(45.0, 90.0, 45.0, 90.0)) < 1e-9
    # 1 degree separation at the equator
    sep = _angular_separation_deg(0.0, 0.0, 0.0, 1.0)
    print(f"  0,0 -> 0,1 az = {sep:.4f} deg")
    assert abs(sep - 1.0) < 1e-6
    # 10 degree alt diff at the same az
    sep2 = _angular_separation_deg(40.0, 180.0, 50.0, 180.0)
    print(f"  40,180 -> 50,180 = {sep2:.4f} deg")
    assert abs(sep2 - 10.0) < 1e-6


def test_extract_alt_az() -> None:
    _hr("2. _extract_alt_az tolerates spelling variants")
    from atlas.safety.park_verify import _extract_alt_az
    # canonical
    assert _extract_alt_az({"altitude": 5.0, "azimuth": 180.0}) == (5.0, 180.0)
    # short
    assert _extract_alt_az({"alt": 5.0, "az": 180.0}) == (5.0, 180.0)
    # snake_case
    assert _extract_alt_az({"alt_deg": 5.0, "az_deg": 180.0}) == (5.0, 180.0)
    # missing -> None, None
    assert _extract_alt_az({"ra": 0, "dec": 0}) == (None, None)
    # bad types
    assert _extract_alt_az({"alt": "not a number", "az": 0}) == (None, None)
    print("  all variants handled")


async def test_verify_happy_path() -> None:
    _hr("3. verify_park happy path (FakeNina parks correctly)")
    from atlas.safety.park_verify import verify_park
    from atlas.simulation.fake_hardware import FakeNina
    nina = FakeNina()
    # Configure where FakeNina will end up after park
    nina._sim_park_alt = 0.0
    nina._sim_park_az = 180.0
    await nina.park()
    # Verify against the same expected
    result = await verify_park(
        nina=nina, expected_alt_deg=0.0, expected_az_deg=180.0,
        tolerance_deg=2.0, settle_s=0.0,
    )
    print(f"  result: verified={result.verified} sep={result.angular_offset_deg} ({result.reason})")
    assert result.verified is True
    assert result.angular_offset_deg is not None
    assert result.angular_offset_deg <= 2.0


async def test_verify_mismatch() -> None:
    _hr("4. verify_park mismatch (FakeNina off by 10 deg)")
    from atlas.safety.park_verify import verify_park
    from atlas.simulation.fake_hardware import FakeNina
    nina = FakeNina()
    nina._sim_park_alt = 10.0   # 10 deg above expected
    nina._sim_park_az = 180.0
    await nina.park()
    result = await verify_park(
        nina=nina, expected_alt_deg=0.0, expected_az_deg=180.0,
        tolerance_deg=2.0, settle_s=0.0,
    )
    print(f"  result: verified={result.verified} sep={result.angular_offset_deg}")
    print(f"  reason: {result.reason}")
    assert result.verified is False
    assert result.angular_offset_deg >= 9.0


async def test_verify_no_alt_az() -> None:
    _hr("5. verify_park: mount that doesn't report alt/az")
    from atlas.safety.park_verify import verify_park

    class _AltAzlessMount:
        async def telescope_info(self):
            return {"connected": True, "ra": 0.0, "dec": 0.0}

    result = await verify_park(
        nina=_AltAzlessMount(),
        expected_alt_deg=0.0, expected_az_deg=180.0,
        tolerance_deg=2.0, settle_s=0.0,
    )
    print(f"  result: verified={result.verified}")
    print(f"  reason: {result.reason}")
    assert result.verified is False
    assert "no alt/az" in result.reason.lower()


async def test_park_and_verify_retries() -> None:
    _hr("6. park_and_verify retries on mismatch")
    from atlas.safety.park_verify import park_and_verify

    class _FlakyMount:
        def __init__(self):
            self._calls = 0
            self._sim_alt = 50.0  # very wrong on first call
            self._sim_az = 180.0

        async def park(self):
            self._calls += 1
            if self._calls >= 3:
                # Third call lands at the right place
                self._sim_alt = 0.0
            return {"ok": True}

        async def telescope_info(self):
            return {"altitude": self._sim_alt, "azimuth": self._sim_az}

    nina = _FlakyMount()
    result = await park_and_verify(
        nina=nina, expected_alt_deg=0.0, expected_az_deg=180.0,
        tolerance_deg=2.0, retries=4, settle_s=0.0,
    )
    print(f"  park calls: {nina._calls}, verified={result.verified}, "
          f"retries_used={result.retries_used}")
    assert result.verified is True
    assert nina._calls >= 3


async def test_emergency_shutdown_audit() -> None:
    _hr("7. EmergencyShutdown emits verify dict in audit")
    from atlas.safety.shutdown import EmergencyShutdown
    from atlas.simulation.fake_hardware import FakeNina
    # FakeNina needs the other methods sequence_stop/dome_close
    nina = FakeNina()

    async def _stop():
        return {"ok": True}
    async def _close():
        return {"ok": True}
    async def _warmup():
        return {"ok": True}
    nina.sequence_stop = _stop
    nina.dome_close = _close
    nina.camera_warmup = _warmup

    # Configure equipment park position via DB (or fall back to defaults)
    from atlas.db.seed import initialise_database
    initialise_database()
    from atlas.db.models import EquipmentProfile
    from atlas.db.session import get_session
    with get_session() as s:
        eq = s.query(EquipmentProfile).first()
        if eq is None:
            eq = EquipmentProfile(camera_type="OSC",
                                       sensor_pixel_size_um=3.76,
                                       focal_length_mm=1000,
                                       aperture_mm=200)
            s.add(eq); s.flush()
        eq.park_alt_deg = 0.0
        eq.park_az_deg = 180.0
        eq.park_tolerance_deg = 2.0
    nina._sim_park_alt = 0.0
    nina._sim_park_az = 180.0
    shutdown = EmergencyShutdown(nina)
    audit = await shutdown.execute(reason="smoke test")
    park_step = next(s for s in audit["steps"] if s["name"] == "park_mount")
    print(f"  park step: ok={park_step['ok']}, "
          f"verify.angular_offset={park_step['verify']['angular_offset_deg']}")
    assert park_step["ok"] is True
    assert "verify" in park_step
    assert park_step["verify"]["verified"] is True


async def main() -> None:
    test_angular_separation()
    test_extract_alt_az()
    await test_verify_happy_path()
    await test_verify_mismatch()
    await test_verify_no_alt_az()
    await test_park_and_verify_retries()
    await test_emergency_shutdown_audit()
    _hr("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
