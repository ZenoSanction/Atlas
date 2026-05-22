"""Mount-park position verification: angular math + dict extraction."""
from __future__ import annotations

import pytest

from atlas.safety.park_verify import (
    _angular_separation_deg, _extract_alt_az, verify_park,
)


def test_zero_separation():
    assert _angular_separation_deg(45.0, 90.0, 45.0, 90.0) == pytest.approx(
        0.0, abs=1e-9
    )


def test_one_degree_az_at_equator():
    sep = _angular_separation_deg(0.0, 0.0, 0.0, 1.0)
    assert sep == pytest.approx(1.0, abs=1e-6)


def test_ten_degree_altitude():
    sep = _angular_separation_deg(40.0, 180.0, 50.0, 180.0)
    assert sep == pytest.approx(10.0, abs=1e-6)


def test_extract_canonical_keys():
    assert _extract_alt_az({"altitude": 5.0, "azimuth": 180.0}) == (5.0, 180.0)


def test_extract_short_keys():
    assert _extract_alt_az({"alt": 5.0, "az": 180.0}) == (5.0, 180.0)


def test_extract_snake_case_keys():
    assert _extract_alt_az({"alt_deg": 5.0, "az_deg": 180.0}) == (5.0, 180.0)


def test_extract_handles_zero_altitude():
    """0.0 is a legitimate alt value (parked at horizon) — don't lose it
    to a falsy-or-default chain."""
    alt, az = _extract_alt_az({"altitude": 0.0, "azimuth": 180.0})
    assert alt == 0.0
    assert az == 180.0


def test_extract_missing_returns_none():
    alt, az = _extract_alt_az({"ra": 0, "dec": 0})
    assert alt is None
    assert az is None


def test_extract_bad_types():
    alt, az = _extract_alt_az({"alt": "not a number", "az": 0})
    assert alt is None
    assert az is None


def test_verify_passes_within_tolerance():
    class _StubMount:
        async def telescope_info(self):
            return {"altitude": 1.0, "azimuth": 180.0}

    import asyncio
    res = asyncio.run(verify_park(
        nina=_StubMount(),
        expected_alt_deg=0.0, expected_az_deg=180.0,
        tolerance_deg=2.0, settle_s=0.0,
    ))
    assert res.verified is True
    assert res.angular_offset_deg < 2.0


def test_verify_fails_out_of_tolerance():
    class _StubMount:
        async def telescope_info(self):
            return {"altitude": 50.0, "azimuth": 180.0}

    import asyncio
    res = asyncio.run(verify_park(
        nina=_StubMount(),
        expected_alt_deg=0.0, expected_az_deg=180.0,
        tolerance_deg=2.0, settle_s=0.0,
    ))
    assert res.verified is False
    assert res.angular_offset_deg > 2.0


def test_verify_when_no_alt_az():
    class _AltAzlessMount:
        async def telescope_info(self):
            return {"ra": 0, "dec": 0}

    import asyncio
    res = asyncio.run(verify_park(
        nina=_AltAzlessMount(),
        expected_alt_deg=0.0, expected_az_deg=180.0,
        tolerance_deg=2.0, settle_s=0.0,
    ))
    assert res.verified is False
    assert "alt/az" in res.reason.lower()
