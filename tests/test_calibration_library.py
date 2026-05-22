"""CalibrationLibrary best-match + coverage logic."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from atlas.calibration.library import (
    BIAS_FRESHNESS_DAYS, DARK_FRESHNESS_DAYS, CalibrationLibrary,
)


def _seed_master(kind, *, gain=100, offset=10, exposure_s=None,
                    ccd_temp_c=None, filter_name=None, days_ago=0):
    from atlas.db.models import CalibrationMaster
    from atlas.db.session import get_session
    with get_session() as s:
        s.add(CalibrationMaster(
            kind=kind, gain=gain, offset=offset,
            exposure_s=exposure_s, ccd_temp_c=ccd_temp_c,
            filter_name=filter_name,
            file_path=f"/fake/{kind}.fit",
            n_frames=20,
            created_at=datetime.utcnow() - timedelta(days=days_ago),
        ))


def _seed_light(filter_name, exposure_s, *, gain=100, offset=10,
                   temp_c=-10.0, days_ago=1):
    from atlas.db.models import Frame, FrameQuality
    from atlas.db.session import get_session
    with get_session() as s:
        s.add(Frame(
            captured_at=datetime.utcnow() - timedelta(days=days_ago),
            file_path=f"/fake/{filter_name}_{exposure_s}.fit",
            frame_type="light", filter_name=filter_name,
            exposure_s=exposure_s, gain=gain, offset=offset,
            ccd_temp_c=temp_c, quality=FrameQuality.UNGRADED,
        ))


def test_empty_library_says_empty(clean_calibration):
    lib = CalibrationLibrary()
    cov = lib.coverage_report()
    assert "EMPTY" in cov.summary
    assert not cov.bias and not cov.dark and not cov.flat


def test_best_match_bias_fresh(clean_calibration):
    _seed_master("bias", gain=100, offset=10, ccd_temp_c=-10.0, days_ago=5)
    lib = CalibrationLibrary()
    m = lib.best_match(kind="bias", gain=100, offset=10, ccd_temp_c=-10.0)
    assert m.master is not None
    assert m.fresh is True


def test_best_match_bias_stale(clean_calibration):
    _seed_master("bias", gain=100, offset=10, ccd_temp_c=-10.0,
                    days_ago=BIAS_FRESHNESS_DAYS + 10)
    lib = CalibrationLibrary()
    m = lib.best_match(kind="bias", gain=100, offset=10, ccd_temp_c=-10.0)
    assert m.master is not None
    assert m.fresh is False
    assert "old" in m.reason


def test_dark_within_exposure_tolerance(clean_calibration):
    _seed_master("dark", gain=100, offset=10, exposure_s=180.0,
                    ccd_temp_c=-10.0, days_ago=5)
    lib = CalibrationLibrary()
    # 200s (within ±20%) -> match
    m = lib.best_match(kind="dark", exposure_s=200.0, gain=100,
                          offset=10, ccd_temp_c=-10.5)
    assert m.master is not None
    # 300s (>20% off) -> no match
    m2 = lib.best_match(kind="dark", exposure_s=300.0, gain=100,
                            offset=10, ccd_temp_c=-10.0)
    assert m2.master is None
    assert "toleranc" in m2.reason.lower()


def test_dark_within_temp_tolerance(clean_calibration):
    _seed_master("dark", gain=100, offset=10, exposure_s=180.0,
                    ccd_temp_c=-10.0, days_ago=5)
    lib = CalibrationLibrary()
    # 180s at -15°C -> 5°C off > ±1°C -> no match
    m = lib.best_match(kind="dark", exposure_s=180.0, gain=100,
                          offset=10, ccd_temp_c=-15.0)
    assert m.master is None


def test_coverage_reports_missing_combos(clean_calibration):
    # Light at gain=100 / 180s / L / -10°C; only a bias master exists
    _seed_master("bias", gain=100, offset=10, ccd_temp_c=-10.0)
    _seed_light("L", 180.0, gain=100, offset=10, temp_c=-10.0)
    _seed_light("R", 60.0, gain=100, offset=10, temp_c=-10.0)

    lib = CalibrationLibrary()
    cov = lib.coverage_report()
    kinds_missing = {m["kind"] for m in cov.missing}
    assert "dark" in kinds_missing
    assert "flat" in kinds_missing
    # Bias is satisfied
    assert "bias" not in kinds_missing


def test_recommended_actions_priority(clean_calibration):
    _seed_light("L", 120.0, gain=100, offset=10, temp_c=-10.0)
    lib = CalibrationLibrary()
    actions = lib.recommended_actions()
    kinds = [a["kind"] for a in actions]
    # Flat should appear before dark, dark before bias
    assert kinds.index("flat") < kinds.index("dark")
    assert kinds.index("dark") < kinds.index("bias")
