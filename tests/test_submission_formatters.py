"""Submission formatter output shape: TNS JSON + NASA EW CSV."""
from __future__ import annotations

import json

import pytest

from atlas.science.submissions.nasa_eo import NasaEoSubmitter
from atlas.science.submissions.tns import (
    TNS_FILTER_CODES, TnsSubmitter,
    _deg_to_sexagesimal_dec, _deg_to_sexagesimal_ra,
)


# ---- TNS ----------------------------------------------------------------

def test_ra_sexagesimal():
    assert _deg_to_sexagesimal_ra(180.0).startswith("12:00:00")
    assert _deg_to_sexagesimal_ra(0.0).startswith("00:00:00")
    assert _deg_to_sexagesimal_ra(15.0).startswith("01:00:00")


def test_dec_sexagesimal_positive():
    assert _deg_to_sexagesimal_dec(45.5).startswith("+45:30")


def test_dec_sexagesimal_negative():
    # Negative dec must carry the minus sign
    s = _deg_to_sexagesimal_dec(-22.25)
    assert s.startswith("-22:15")


def test_tns_filter_codes_baseline():
    assert TNS_FILTER_CODES["L"] == 22
    assert TNS_FILTER_CODES["R"] == 13
    assert TNS_FILTER_CODES["V"] == 11
    assert TNS_FILTER_CODES["Ha"] == 21


def test_tns_format_required_keys():
    sub = TnsSubmitter(reporting_group_id=74, instrument_id=104)
    payload = sub.format({
        "value": {
            "ra_deg": 180.5, "dec_deg": -25.3,
            "ra_err_arcsec": 0.4, "dec_err_arcsec": 0.4,
            "mag": 18.7, "mag_err": 0.05, "filter": "L",
            "exposure_s": 120,
            "discovery_datetime_utc": "2026-05-22T04:30:00",
        },
    })
    parsed = json.loads(payload.text)
    assert "at_report" in parsed
    rep = parsed["at_report"]["0"]
    for key in ("ra", "dec", "reporting_group_id",
                  "discovery_datetime", "at_type", "photometry"):
        assert key in rep, f"TNS report missing {key}"
    # filter L -> code 22
    photo0 = rep["photometry"]["photometry_group"]["0"]
    assert photo0["filter_value"] == 22
    assert photo0["flux"] == 18.7


def test_tns_format_with_non_detection():
    sub = TnsSubmitter()
    payload = sub.format({
        "value": {
            "ra_deg": 100.0, "dec_deg": 30.0,
            "mag": 19.5, "mag_err": 0.08, "filter": "V",
            "exposure_s": 60,
            "discovery_datetime_utc": "2026-05-22T05:00:00",
            "non_detection": {
                "obsdate_utc": "2026-05-15T05:00:00",
                "limiting_mag": 21.5, "filter": "L",
            },
        },
    })
    parsed = json.loads(payload.text)
    rep = parsed["at_report"]["0"]
    assert "non_detection" in rep
    assert rep["non_detection"]["limiting_flux"] == 21.5


# ---- NASA Exoplanet Watch ------------------------------------------------

def test_nasa_eo_csv_has_type_header():
    sub = NasaEoSubmitter()
    payload = sub.format({"value": {
        "star_name": "WASP-12", "exoplanet_name": "WASP-12 b",
        "filter": "V", "rows": [],
    }})
    lines = payload.text.splitlines()
    assert lines[0] == "#TYPE=EXOPLANET"
    assert any(l.startswith("#STAR_NAME=") for l in lines)
    assert any(l.startswith("#EXOPLANET_NAME=") for l in lines)


def test_nasa_eo_csv_row_count_matches():
    sub = NasaEoSubmitter()
    rows = [
        {"bjd_tdb": 2461125.2 + i / 1440.0,
            "rel_flux": 1.0001, "rel_flux_err": 0.001,
            "airmass": 1.2, "exposure_s": 60.0,
            "comp_fluxes": [1.0, 1.0]}
        for i in range(20)
    ]
    payload = sub.format({"value": {
        "star_name": "TOI-1", "exoplanet_name": "TOI-1 b",
        "filter": "Rc", "rows": rows,
    }})
    lines = payload.text.splitlines()
    col_idx = next(i for i, l in enumerate(lines)
                       if l.startswith("BJD_TDB"))
    data = [l for l in lines[col_idx + 1:] if l.strip()]
    assert len(data) == 20
    assert payload.metadata["n_rows"] == 20


def test_nasa_eo_columns_match_comp_star_count():
    sub = NasaEoSubmitter()
    rows = [{"bjd_tdb": 2461125.2, "rel_flux": 1.0,
                "rel_flux_err": 0.001, "airmass": 1.2,
                "exposure_s": 60.0, "comp_fluxes": [1.0, 1.0, 1.0]}]
    payload = sub.format({"value": {
        "star_name": "X", "exoplanet_name": "X b",
        "filter": "V", "rows": rows,
    }})
    col_line = next(l for l in payload.text.splitlines()
                        if l.startswith("BJD_TDB"))
    assert "COMP_FLUX_1" in col_line
    assert "COMP_FLUX_3" in col_line
    assert "COMP_FLUX_4" not in col_line


def test_nasa_eo_no_comp_stars_no_comp_columns():
    sub = NasaEoSubmitter()
    payload = sub.format({"value": {
        "star_name": "X", "exoplanet_name": "X b",
        "filter": "V", "rows": [
            {"bjd_tdb": 2461125.2, "rel_flux": 1.0,
                "rel_flux_err": 0.001, "airmass": 1.2, "exposure_s": 60},
        ],
    }})
    col_line = next(l for l in payload.text.splitlines()
                        if l.startswith("BJD_TDB"))
    assert "COMP_FLUX" not in col_line
