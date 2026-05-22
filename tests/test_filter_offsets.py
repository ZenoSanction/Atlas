"""Filter focus offset decision engine."""
from __future__ import annotations

import pytest

from atlas.focus.offsets import (
    FilterOffsetTable, OFFSET_FRESHNESS_DAYS, decide_filter_change,
)


def test_empty_table_forces_af(equipment_with_offsets):
    d = decide_filter_change(from_filter="L", to_filter="R")
    assert d.action == "autofocus"


def test_reference_always_afs(equipment_with_offsets):
    tbl = equipment_with_offsets
    tbl.record_af(filter_name="L", focuser_step=15000)
    d = decide_filter_change(from_filter="R", to_filter="L")
    assert d.action == "autofocus"
    assert "reference" in d.reason.lower()


def test_jump_with_known_offset(equipment_with_offsets):
    tbl = equipment_with_offsets
    tbl.record_af(filter_name="L", focuser_step=15000)
    tbl.record_af(filter_name="R", focuser_step=14988)
    d = decide_filter_change(from_filter="L", to_filter="R")
    assert d.action == "jump"
    assert d.target_step == 14988
    assert d.offset_applied == -12


def test_multiple_filters_each_jump(equipment_with_offsets):
    tbl = equipment_with_offsets
    tbl.record_af(filter_name="L", focuser_step=15000)
    for filt, step, expect in [
        ("R", 14988, 14988),
        ("G", 14992, 14992),
        ("B", 15005, 15005),
    ]:
        tbl.record_af(filter_name=filt, focuser_step=step)
        d = decide_filter_change(from_filter="L", to_filter=filt)
        assert d.action == "jump"
        assert d.target_step == expect


def test_stale_forces_af(equipment_with_offsets):
    from datetime import datetime, timedelta
    from atlas.db.models import EquipmentProfile
    from atlas.db.session import get_session

    tbl = equipment_with_offsets
    tbl.record_af(filter_name="L", focuser_step=15000)
    tbl.record_af(filter_name="R", focuser_step=14988)
    # Backdate all entries so the freshness check fails
    old = (
        datetime.utcnow() - timedelta(days=OFFSET_FRESHNESS_DAYS + 10)
    ).isoformat(timespec="seconds") + "Z"
    with get_session() as s:
        eq = s.query(EquipmentProfile).first()
        rebuilt = {}
        for k, v in (eq.filter_offsets or {}).items():
            inner = dict(v)
            inner["recorded_at"] = old
            rebuilt[k] = inner
        eq.filter_offsets = rebuilt

    d = decide_filter_change(from_filter="L", to_filter="R")
    assert d.action == "autofocus"
    assert "stale" in d.reason.lower()


def test_same_filter_no_change(equipment_with_offsets):
    d = decide_filter_change(from_filter="L", to_filter="L")
    assert d.action == "no_change"


def test_offset_for_unknown_filter(equipment_with_offsets):
    tbl = equipment_with_offsets
    tbl.record_af(filter_name="L", focuser_step=15000)
    # Asking for X (never recorded) -> autofocus
    d = decide_filter_change(from_filter="L", to_filter="X")
    assert d.action == "autofocus"
