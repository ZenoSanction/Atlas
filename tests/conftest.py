"""Pytest fixtures shared across the decision-logic suite.

These tests exercise the pure-logic engines (autofocus, platesolve,
workflow policies, calibration matching, etc.). Each fixture sets up
the minimum state the test needs and tears it down so tests are
isolated even when they all touch the same SQLite DB.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is importable when pytest is launched
# from anywhere (CI vs local).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_db():
    """Run schema migration once before any DB-touching test."""
    from atlas.db.seed import initialise_database
    initialise_database()
    yield


@pytest.fixture
def clean_calibration():
    """Wipe calibration_masters + frames before & after the test."""
    from atlas.db.models import CalibrationMaster, Frame
    from atlas.db.session import get_session

    def _wipe():
        with get_session() as s:
            s.query(CalibrationMaster).delete()
            s.query(Frame).delete()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def clean_campaigns():
    """Wipe campaign + frame data so continuity tests start fresh."""
    from atlas.db.models import (
        Campaign, CampaignTarget, Frame, Target,
    )
    from atlas.db.session import get_session

    def _wipe():
        with get_session() as s:
            s.query(Frame).delete()
            s.query(CampaignTarget).delete()
            s.query(Campaign).delete()
            s.query(Target).delete()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def equipment_with_offsets():
    """Ensure EquipmentProfile exists with a known reference filter
    and an empty offset table. Yields the FilterOffsetTable helper."""
    from atlas.db.models import EquipmentProfile
    from atlas.db.session import get_session
    from atlas.focus.offsets import FilterOffsetTable

    with get_session() as s:
        eq = s.query(EquipmentProfile).first()
        if eq is None:
            eq = EquipmentProfile(
                camera_type="MONO", sensor_pixel_size_um=3.76,
                focal_length_mm=1000.0, aperture_mm=200.0,
                filter_offset_reference="L",
            )
            s.add(eq)
            s.flush()
        eq.filter_offset_reference = "L"
        eq.filter_offsets = {}
    tbl = FilterOffsetTable()
    yield tbl
    # Reset
    with get_session() as s:
        eq = s.query(EquipmentProfile).first()
        if eq is not None:
            eq.filter_offsets = {}
