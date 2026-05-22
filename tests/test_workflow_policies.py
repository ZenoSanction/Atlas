"""Workflow policy distinctness — each science mode has the right
policy bundle and no two are accidentally identical."""
from __future__ import annotations

import pytest

from atlas.db.models import WorkflowKind
from atlas.workflows.registry import WORKFLOWS, get_workflow


def test_every_kind_has_a_workflow():
    expected = set(WorkflowKind)
    registered = set(WORKFLOWS.keys())
    assert expected == registered


def test_string_lookup_works():
    wf = get_workflow("deepsky")
    assert wf.kind == WorkflowKind.DEEPSKY


def test_unknown_falls_back_to_deepsky():
    wf = get_workflow("definitely-not-a-real-workflow")
    assert wf.kind == WorkflowKind.DEEPSKY


def test_deepsky_dithers_and_AFs_often():
    spec = get_workflow("deepsky").plan(target={}, conditions={})
    assert spec.dither.enabled is True
    assert spec.dither.every_n_frames == 1
    assert spec.autofocus.on_filter_change is True
    assert spec.autofocus.time_interval_min == 60.0
    assert spec.platesolve.enabled is True
    assert spec.platesolve.every_frame is False  # solve once, not every frame


def test_photometry_locks_focus_and_no_dither():
    spec = get_workflow("photometry").plan(target={}, conditions={})
    assert spec.dither.enabled is False
    # NO time-based refocus; the only re-AF is on filter change
    assert spec.autofocus.time_interval_min is None
    # HFR trigger should be loose
    assert spec.autofocus.hfr_drift_factor >= 2.0


def test_exoplanet_locks_everything():
    spec = get_workflow("exoplanet").plan(target={}, conditions={})
    assert spec.dither.enabled is False
    assert spec.autofocus.on_filter_change is False
    assert spec.autofocus.time_interval_min is None
    assert spec.autofocus.hfr_drift_factor >= 99.0
    assert spec.extras.get("locked_focus") is True


def test_astrometry_per_frame_solve():
    spec = get_workflow("astrometry").plan(target={}, conditions={})
    assert spec.platesolve.every_frame is True
    assert spec.dither.enabled is False
    assert (spec.acceptance.min_star_count or 0) >= 100


def test_transient_per_frame_solve_with_dither():
    spec = get_workflow("transient").plan(target={}, conditions={})
    assert spec.platesolve.every_frame is True
    assert spec.dither.enabled is True
    # tight eccentricity for PSF subtraction
    assert (spec.acceptance.max_eccentricity or 1.0) <= 0.6


def test_planetary_disables_solve_and_dither():
    spec = get_workflow("planetary").plan(target={}, conditions={})
    assert spec.platesolve.enabled is False
    assert spec.dither.enabled is False
    assert spec.extras.get("software_mode") == "sharpcap"


def test_all_six_policies_distinct():
    seen: set = set()
    for kind, cls in WORKFLOWS.items():
        spec = cls().plan(target={}, conditions={})
        sig = (
            spec.autofocus.on_filter_change,
            spec.autofocus.time_interval_min,
            spec.autofocus.hfr_drift_factor,
            spec.platesolve.enabled,
            spec.platesolve.every_frame,
            spec.dither.enabled,
            spec.dither.every_n_frames,
        )
        assert sig not in seen, (
            f"{kind.value} has duplicate policy signature {sig}"
        )
        seen.add(sig)
    assert len(seen) == len(WORKFLOWS)
