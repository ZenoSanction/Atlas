"""Smoke test for the per-filter focuser-offset table.

Tests:
  1. Fresh DB: no offsets, every filter change forces AF.
  2. Record L AF (reference) at step 15000. Now L itself still AFs.
  3. Record R AF at step 14988. Offset for R becomes -12.
     decide_filter_change L->R now returns "jump" with target 14988.
  4. Record G AF at step 14992. Offset = -8.
  5. Stale entry (simulated by direct DB tweak) -> back to AF.
  6. no_change when from == to.

Run from project root:
    venv\\Scripts\\python.exe scripts\\smoke_filter_offsets.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def _clean_offsets() -> None:
    from atlas.db.models import EquipmentProfile
    from atlas.db.session import get_session
    with get_session() as s:
        eq = s.query(EquipmentProfile).first()
        if eq is not None:
            eq.filter_offsets = {}
            eq.filter_offset_reference = "L"


def _ensure_equipment() -> None:
    from atlas.db.models import EquipmentProfile
    from atlas.db.session import get_session
    with get_session() as s:
        if s.query(EquipmentProfile).first() is None:
            s.add(EquipmentProfile(
                camera_type="MONO",
                sensor_pixel_size_um=3.76,
                focal_length_mm=1000.0, aperture_mm=200.0,
                cooling_setpoint_c=-10.0,
                filter_offset_reference="L",
                filter_offsets={},
            ))


def test_empty_table() -> None:
    _hr("1. empty table -> every change forces AF")
    _ensure_equipment()
    _clean_offsets()
    from atlas.focus.offsets import decide_filter_change
    d = decide_filter_change(from_filter="L", to_filter="R")
    print(f"  L -> R: action={d.action}  ({d.reason})")
    assert d.action == "autofocus"


def test_reference_always_afs() -> None:
    _hr("2. reference filter (L) always AFs")
    _ensure_equipment()
    _clean_offsets()
    from atlas.focus.offsets import FilterOffsetTable, decide_filter_change
    tbl = FilterOffsetTable()
    # Record L at step 15000
    tbl.record_af(filter_name="L", focuser_step=15000, af_hfr=2.1,
                    temperature_c=-5.0)
    # Even with L recorded, switching X->L should AF (refresh baseline)
    d = decide_filter_change(from_filter="R", to_filter="L")
    print(f"  R -> L: action={d.action}  ({d.reason})")
    assert d.action == "autofocus"
    assert "reference" in d.reason.lower()


def test_jump_with_known_offset() -> None:
    _hr("3. L->R with known offset jumps without AF")
    _ensure_equipment()
    _clean_offsets()
    from atlas.focus.offsets import FilterOffsetTable, decide_filter_change
    tbl = FilterOffsetTable()
    # L baseline at 15000
    tbl.record_af(filter_name="L", focuser_step=15000, af_hfr=2.1)
    # R at 14988 -> offset = -12 vs L
    tbl.record_af(filter_name="R", focuser_step=14988, af_hfr=2.2)
    entry = tbl.get("R")
    assert entry is not None
    print(f"  R recorded: offset={entry.offset_steps}, "
            f"ref={entry.reference_filter}")
    assert entry.offset_steps == -12
    # Now decide L -> R
    d = decide_filter_change(from_filter="L", to_filter="R")
    print(f"  L -> R: action={d.action}  target={d.target_step}  "
            f"offset={d.offset_applied}  ({d.reason})")
    assert d.action == "jump"
    assert d.target_step == 14988
    assert d.offset_applied == -12


def test_multiple_filters() -> None:
    _hr("4. multiple recorded filters all produce jumps")
    _ensure_equipment()
    _clean_offsets()
    from atlas.focus.offsets import FilterOffsetTable, decide_filter_change
    tbl = FilterOffsetTable()
    tbl.record_af(filter_name="L", focuser_step=15000)
    tbl.record_af(filter_name="R", focuser_step=14988)
    tbl.record_af(filter_name="G", focuser_step=14992)
    tbl.record_af(filter_name="B", focuser_step=15005)

    for filt, expected_step in [("R", 14988), ("G", 14992), ("B", 15005)]:
        d = decide_filter_change(from_filter="L", to_filter=filt)
        print(f"  L -> {filt}: action={d.action}  target={d.target_step}")
        assert d.action == "jump"
        assert d.target_step == expected_step


def test_stale_offset() -> None:
    _hr("5. stale offset forces AF")
    _ensure_equipment()
    _clean_offsets()
    from atlas.db.models import EquipmentProfile
    from atlas.db.session import get_session
    from atlas.focus.offsets import (
        FilterOffsetTable, OFFSET_FRESHNESS_DAYS, decide_filter_change,
    )
    tbl = FilterOffsetTable()
    tbl.record_af(filter_name="L", focuser_step=15000)
    tbl.record_af(filter_name="R", focuser_step=14988)
    # Tweak recorded_at to be 60 days ago — build a FRESH dict-of-dicts
    # so SQLAlchemy's JSON column sees the change.
    old = (datetime.utcnow() - timedelta(days=60)).isoformat(timespec="seconds") + "Z"
    with get_session() as s:
        eq = s.query(EquipmentProfile).first()
        rebuilt: dict = {}
        for k, v in (eq.filter_offsets or {}).items():
            inner = dict(v)
            inner["recorded_at"] = old
            rebuilt[k] = inner
        eq.filter_offsets = rebuilt

    d = decide_filter_change(from_filter="L", to_filter="R")
    print(f"  L -> R (60d old): action={d.action}  ({d.reason})")
    assert d.action == "autofocus"
    assert "stale" in d.reason.lower()


def test_no_change() -> None:
    _hr("6. same-filter change -> no_change")
    _ensure_equipment()
    from atlas.focus.offsets import decide_filter_change
    d = decide_filter_change(from_filter="L", to_filter="L")
    print(f"  L -> L: action={d.action}  ({d.reason})")
    assert d.action == "no_change"


def main() -> None:
    # Make sure DB schema has filter_offsets column
    from atlas.db.seed import initialise_database
    initialise_database()

    test_empty_table()
    test_reference_always_afs()
    test_jump_with_known_offset()
    test_multiple_filters()
    test_stale_offset()
    test_no_change()
    _hr("ALL SMOKE TESTS PASSED")
    _clean_offsets()
    print("  (test rows cleaned)")


if __name__ == "__main__":
    main()
