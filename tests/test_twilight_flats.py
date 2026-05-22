"""Twilight flat orchestrator: exposure tuning + ADU acceptance band."""
from __future__ import annotations

import pytest

from atlas.calibration.twilight_flats import (
    ADU_TOLERANCE_PCT, TARGET_ADU, TwilightFlatOrchestrator,
)
from atlas.simulation.fake_hardware import FakeNina


@pytest.fixture
def orch():
    return TwilightFlatOrchestrator(
        nina=FakeNina(), latitude_deg=40.75, longitude_deg=-73.98,
        simulation=True,
    )


def test_exposure_tune_doubles_when_too_dim(orch):
    new = orch._adjust_exposure(current_exposure=1.0, measured_adu=15000.0)
    # target/measured = 30k/15k = 2x
    assert new == pytest.approx(2.0, abs=0.05)


def test_exposure_tune_halves_when_too_bright(orch):
    new = orch._adjust_exposure(current_exposure=1.0, measured_adu=60000.0)
    assert new == pytest.approx(0.5, abs=0.05)


def test_exposure_clamps_at_min(orch):
    # 0.5s at 120k ADU would want 0.125s — must clamp at 0.5
    new = orch._adjust_exposure(current_exposure=0.5, measured_adu=120000.0)
    assert new == 0.5


def test_exposure_clamps_at_max(orch):
    # 30s at 100 ADU would want 9000s — must clamp at 30
    new = orch._adjust_exposure(current_exposure=30.0, measured_adu=100.0)
    assert new == 30.0


def test_acceptance_band_center(orch):
    assert orch._frame_accepted(TARGET_ADU) is True


def test_acceptance_band_edges(orch):
    lo = TARGET_ADU * (1.0 - ADU_TOLERANCE_PCT / 100.0)
    hi = TARGET_ADU * (1.0 + ADU_TOLERANCE_PCT / 100.0)
    assert orch._frame_accepted(lo + 1) is True
    assert orch._frame_accepted(hi - 1) is True
    assert orch._frame_accepted(lo - 1) is False
    assert orch._frame_accepted(hi + 1) is False


def test_acceptance_rejects_none(orch):
    assert orch._frame_accepted(None) is False


def test_acceptance_rejects_zero(orch):
    assert orch._frame_accepted(0) is False
