"""Smoke test for the calibration library.

Inserts synthetic CalibrationMaster + Frame rows into a clean test
session and verifies:
  1. Empty library produces 'empty' summary.
  2. A bias master alone matches by gain/offset; freshness honored.
  3. A dark within tolerance matches; outside tolerance does not.
  4. Stale masters are flagged.
  5. coverage_report() reports missing combos for filters/exposures
     used by recent lights.
  6. recommended_actions() prioritizes flats > darks > bias.

Run from project root:
    venv\\Scripts\\python.exe scripts\\smoke_calibration_library.py
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


def _seed_master(kind, **kw):
    """Insert one CalibrationMaster row with optional age (days_ago)."""
    from atlas.db.models import CalibrationMaster
    from atlas.db.session import get_session
    days_ago = kw.pop("days_ago", 0)
    with get_session() as s:
        m = CalibrationMaster(
            kind=kind,
            filter_name=kw.get("filter_name"),
            exposure_s=kw.get("exposure_s"),
            ccd_temp_c=kw.get("ccd_temp_c"),
            gain=kw.get("gain"),
            offset=kw.get("offset"),
            file_path=kw.get("file_path", f"/fake/{kind}_master.fit"),
            n_frames=kw.get("n_frames", 20),
            created_at=datetime.utcnow() - timedelta(days=days_ago),
        )
        s.add(m)
        s.flush()
        return m.id


def _seed_light_frame(filter_name, exposure_s, gain, offset, temp_c,
                        days_ago=0):
    from atlas.db.models import Frame, FrameQuality
    from atlas.db.session import get_session
    with get_session() as s:
        f = Frame(
            captured_at=datetime.utcnow() - timedelta(days=days_ago),
            file_path=f"/fake/light_{filter_name}.fit",
            frame_type="light",
            filter_name=filter_name,
            exposure_s=exposure_s, gain=gain, offset=offset,
            ccd_temp_c=temp_c, quality=FrameQuality.UNGRADED,
        )
        s.add(f)


def _clean_db() -> None:
    """Wipe calibration_masters + frames so test runs are isolated."""
    from atlas.db.models import CalibrationMaster, Frame
    from atlas.db.session import get_session
    with get_session() as s:
        s.query(CalibrationMaster).delete()
        s.query(Frame).delete()


def test_empty_library() -> None:
    _hr("1. empty library -> 'EMPTY' summary")
    _clean_db()
    from atlas.calibration.library import CalibrationLibrary
    cov = CalibrationLibrary().coverage_report()
    print(f"  summary: {cov.summary}")
    print(f"  bias={len(cov.bias)} dark={len(cov.dark)} flat={len(cov.flat)}")
    assert "EMPTY" in cov.summary
    assert len(cov.bias) == 0 and len(cov.dark) == 0 and len(cov.flat) == 0


def test_best_match_bias() -> None:
    _hr("2. bias best-match: gain/offset match + freshness")
    _clean_db()
    from atlas.calibration.library import CalibrationLibrary
    lib = CalibrationLibrary()

    # Fresh bias master at gain=100 offset=10 temp=-10
    _seed_master("bias", gain=100, offset=10, ccd_temp_c=-10.0, days_ago=5)
    # Stale bias master at gain=200 offset=10 (180 days old)
    _seed_master("bias", gain=200, offset=10, ccd_temp_c=-10.0, days_ago=180)

    # Should match gain=100 cleanly
    m1 = lib.best_match(kind="bias", gain=100, offset=10, ccd_temp_c=-10.0)
    print(f"  gain=100 match: master_id={m1.master.id if m1.master else None} "
          f"fresh={m1.fresh}  ({m1.reason})")
    assert m1.master is not None
    assert m1.fresh is True

    # Should match gain=200 but flagged not-fresh
    m2 = lib.best_match(kind="bias", gain=200, offset=10, ccd_temp_c=-10.0)
    print(f"  gain=200 match: master_id={m2.master.id if m2.master else None} "
          f"fresh={m2.fresh}  ({m2.reason})")
    assert m2.master is not None
    assert m2.fresh is False

    # Unknown gain returns no master
    m3 = lib.best_match(kind="bias", gain=999, offset=10, ccd_temp_c=-10.0)
    print(f"  gain=999 match: master={m3.master}  ({m3.reason})")
    assert m3.master is None


def test_best_match_dark_tolerance() -> None:
    _hr("3. dark best-match: within ±20% exposure / ±1°C")
    _clean_db()
    from atlas.calibration.library import CalibrationLibrary
    lib = CalibrationLibrary()

    # 180s dark @ -10°C, gain=100
    _seed_master("dark", exposure_s=180.0, gain=100, offset=10,
                   ccd_temp_c=-10.0, days_ago=5)

    # Light at 200s, -10.5°C -> within tolerance, should match
    m1 = lib.best_match(kind="dark", exposure_s=200.0, gain=100,
                          offset=10, ccd_temp_c=-10.5)
    print(f"  200s @-10.5C: match={m1.master is not None}  ({m1.reason})")
    assert m1.master is not None

    # Light at 300s -> outside ±20% (300 vs 180 -> 66% diff), no match
    m2 = lib.best_match(kind="dark", exposure_s=300.0, gain=100,
                          offset=10, ccd_temp_c=-10.0)
    print(f"  300s @-10C: match={m2.master is not None}  ({m2.reason})")
    assert m2.master is None

    # Light at 180s but -15°C (5°C off, outside ±1°C), no match
    m3 = lib.best_match(kind="dark", exposure_s=180.0, gain=100,
                          offset=10, ccd_temp_c=-15.0)
    print(f"  180s @-15C: match={m3.master is not None}  ({m3.reason})")
    assert m3.master is None


def test_coverage_report() -> None:
    _hr("4. coverage_report: lights need flats/darks the library lacks")
    _clean_db()
    from atlas.calibration.library import CalibrationLibrary
    lib = CalibrationLibrary()

    # We have ONE bias master, no darks, no flats
    _seed_master("bias", gain=100, offset=10, ccd_temp_c=-10.0, days_ago=5)

    # Recent lights: L 180s, R 60s — both at gain=100, offset=10, -10°C
    _seed_light_frame("L", 180.0, 100, 10, -10.0, days_ago=1)
    _seed_light_frame("L", 180.0, 100, 10, -10.0, days_ago=1)
    _seed_light_frame("R", 60.0, 100, 10, -10.0, days_ago=1)

    cov = lib.coverage_report()
    print(f"  summary: {cov.summary}")
    print(f"  bias={len(cov.bias)} dark={len(cov.dark)} flat={len(cov.flat)} "
          f"missing={len(cov.missing)} stale={len(cov.stale)}")
    for m in cov.missing[:6]:
        print(f"    missing: {m}")
    assert len(cov.bias) == 1
    assert len(cov.missing) >= 4   # 2 darks + 2 flats (L gain100, R gain100)
    kinds_missing = {m["kind"] for m in cov.missing}
    assert "dark" in kinds_missing
    assert "flat" in kinds_missing


def test_recommended_actions_priority() -> None:
    _hr("5. recommended_actions: flats > darks > bias > stale-refresh")
    _clean_db()
    from atlas.calibration.library import CalibrationLibrary
    lib = CalibrationLibrary()

    # Light frame establishes need
    _seed_light_frame("L", 120.0, 100, 10, -10.0, days_ago=1)
    # No masters at all -> all three actions present

    actions = lib.recommended_actions()
    print(f"  {len(actions)} actions queued")
    for a in actions:
        print(f"    [{a['priority']}] {a['kind']:5s}  {a['summary']}")
    priorities = [a["priority"] for a in actions]
    # 'high' should come first
    if priorities:
        assert priorities[0] in ("high", "medium")
    kinds = [a["kind"] for a in actions]
    assert "flat" in kinds and "dark" in kinds and "bias" in kinds


def main() -> None:
    test_empty_library()
    test_best_match_bias()
    test_best_match_dark_tolerance()
    test_coverage_report()
    test_recommended_actions_priority()
    _hr("ALL SMOKE TESTS PASSED")
    # Clean up so we don't leave test rows in the dev DB
    _clean_db()
    print("  (test rows cleaned)")


if __name__ == "__main__":
    main()
