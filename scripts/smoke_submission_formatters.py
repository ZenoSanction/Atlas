"""Smoke test for TNS + NASA Exoplanet Watch formatters.

Tests:
  1. RA/Dec sexagesimal conversion math.
  2. TNS format() produces valid JSON with all required keys.
  3. TNS filter code lookup.
  4. NASA EW format() produces a CSV with proper #TYPE header.
  5. NASA EW row count matches measurement row count.
  6. NASA EW comp star count adapts to data.

Run from project root:
    venv\\Scripts\\python.exe scripts\\smoke_submission_formatters.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def test_sexagesimal_conversion() -> None:
    _hr("1. RA/Dec sexagesimal conversion")
    from atlas.science.submissions.tns import (
        _deg_to_sexagesimal_ra, _deg_to_sexagesimal_dec,
    )
    # 12h00m00.0s = 180.0 deg
    ra = _deg_to_sexagesimal_ra(180.0)
    print(f"  180.0 deg RA -> {ra}")
    assert ra.startswith("12:00:00")

    # 45.5 deg = +45 deg 30 min
    dec = _deg_to_sexagesimal_dec(45.5)
    print(f"  +45.5 deg Dec -> {dec}")
    assert dec.startswith("+45:30")

    # -22.25 deg = -22 deg 15 min
    dec_neg = _deg_to_sexagesimal_dec(-22.25)
    print(f"  -22.25 deg Dec -> {dec_neg}")
    assert dec_neg.startswith("-22:15")


def test_tns_format() -> None:
    _hr("2. TNS format() builds valid JSON with all required keys")
    from atlas.science.submissions.tns import TnsSubmitter
    sub = TnsSubmitter(reporting_group_id=74, instrument_id=104,
                          bot_name="ATLAS")
    measurement = {
        "value": {
            "ra_deg": 180.5, "dec_deg": -25.3,
            "ra_err_arcsec": 0.4, "dec_err_arcsec": 0.4,
            "mag": 18.7, "mag_err": 0.05,
            "filter": "L",
            "snr": 12.0, "exposure_s": 120,
            "discovery_datetime_utc": "2026-05-22T04:30:00",
            "internal_name": "ATLAS25abc",
            "host_name": "NGC 1234",
            "host_redshift": 0.012,
            "remarks": "Detected vs PS1 reference",
            "photometry_comment": "first detection",
            "non_detection": {
                "obsdate_utc": "2026-05-15T04:30:00",
                "limiting_mag": 21.0, "filter": "L",
            },
        },
    }
    payload = sub.format(measurement)
    print(f"  content_type: {payload.content_type}")
    print(f"  metadata: {payload.metadata}")
    print(f"  json length: {len(payload.text)} chars")
    parsed = json.loads(payload.text)
    print(f"  top-level keys: {list(parsed.keys())}")
    assert "at_report" in parsed
    report0 = parsed["at_report"]["0"]
    assert report0["reporting_group_id"] == 74
    assert "ra" in report0 and "value" in report0["ra"]
    assert report0["ra"]["value"].startswith("12:")
    assert report0["dec"]["value"].startswith("-25:")
    # filter L should map to TNS filter code 22
    photo = report0["photometry"]["photometry_group"]["0"]
    assert photo["filter_value"] == 22
    assert photo["flux"] == 18.7
    # non_detection block present
    assert "non_detection" in report0
    assert report0["non_detection"]["limiting_flux"] == 21.0
    print("  [OK] all required TNS fields present")


def test_tns_filter_codes() -> None:
    _hr("3. TNS filter code lookup")
    from atlas.science.submissions.tns import TNS_FILTER_CODES
    print(f"  available filters: {sorted(TNS_FILTER_CODES.keys())}")
    assert TNS_FILTER_CODES["L"] == 22
    assert TNS_FILTER_CODES["R"] == 13
    assert TNS_FILTER_CODES["Ha"] == 21
    assert TNS_FILTER_CODES["V"] == 11


def test_nasa_eo_format() -> None:
    _hr("4. NASA Exoplanet Watch CSV with header block")
    from atlas.science.submissions.nasa_eo import NasaEoSubmitter
    sub = NasaEoSubmitter(obscode="ATLAS01",
                              detector="ASI2600MM",
                              pixel_scale_arcsec=0.5)
    rows = []
    base_bjd = 2461125.20000
    for i in range(120):  # 2 hour transit window at 1-min cadence
        rows.append({
            "bjd_tdb": base_bjd + (i / 1440.0),
            "rel_flux": 1.0001 - (0.005 * (1 if 30 < i < 90 else 0)),
            "rel_flux_err": 0.0009,
            "airmass": 1.21 + (i / 5000.0),
            "exposure_s": 60.0,
            "comp_fluxes": [1.0023, 1.0019, 1.0031],
        })
    measurement = {
        "value": {
            "star_name": "TOI-1234.01",
            "exoplanet_name": "TOI-1234 b",
            "filter": "Rc",
            "binning": "1x1",
            "priors_source": "ExoFOP-TESS",
            "comp_stars": [
                {"id": "TIC100", "mag": 12.5},
                {"id": "TIC200", "mag": 12.8},
                {"id": "TIC300", "mag": 13.1},
            ],
            "remarks": "Clear, good seeing",
            "rows": rows,
        },
    }
    payload = sub.format(measurement)
    print(f"  content_type: {payload.content_type}")
    print(f"  metadata: {payload.metadata}")
    print(f"  csv length: {len(payload.text)} chars")
    lines = payload.text.splitlines()
    print(f"  first 8 lines:")
    for line in lines[:8]:
        print(f"    {line}")
    assert lines[0] == "#TYPE=EXOPLANET"
    assert "#OBSCODE=ATLAS01" in lines
    assert "#STAR_NAME=TOI-1234.01" in lines
    assert "#EXOPLANET_NAME=TOI-1234 b" in lines
    assert "#FILTER=Rc" in lines
    assert any("#COMMENT=Comp star: TIC100" in l for l in lines)
    # Column header line
    column_line = next(l for l in lines
                          if l.startswith("BJD_TDB"))
    print(f"  columns: {column_line}")
    assert "COMP_FLUX_1" in column_line
    assert "COMP_FLUX_3" in column_line
    # Count data rows (after column header)
    col_idx = lines.index(column_line)
    data_rows = [l for l in lines[col_idx + 1:] if l.strip()]
    print(f"  data rows: {len(data_rows)}")
    assert len(data_rows) == 120


def test_nasa_eo_no_comp_stars() -> None:
    _hr("5. NASA EW with zero comp stars -> still valid CSV")
    from atlas.science.submissions.nasa_eo import NasaEoSubmitter
    sub = NasaEoSubmitter()
    payload = sub.format({
        "value": {
            "star_name": "WASP-12",
            "exoplanet_name": "WASP-12 b",
            "filter": "V",
            "rows": [{"bjd_tdb": 2461125.2, "rel_flux": 1.0,
                         "rel_flux_err": 0.001, "airmass": 1.1,
                         "exposure_s": 30}],
        },
    })
    print(f"  metadata: {payload.metadata}")
    assert payload.metadata["n_comp_stars"] == 0
    assert payload.metadata["n_rows"] == 1
    column_line = next(l for l in payload.text.splitlines()
                          if l.startswith("BJD_TDB"))
    assert "COMP_FLUX" not in column_line, \
        "no comp stars -> no COMP_FLUX columns"


def main() -> None:
    test_sexagesimal_conversion()
    test_tns_format()
    test_tns_filter_codes()
    test_nasa_eo_format()
    test_nasa_eo_no_comp_stars()
    _hr("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
