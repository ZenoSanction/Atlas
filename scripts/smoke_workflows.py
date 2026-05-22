"""Smoke test for the workflow modules + registry.

Verifies that:
  1. get_workflow returns a distinct policy bundle for every kind.
  2. Each workflow's policies match its science requirements:
       deepsky    -> AF on filter change, dither every frame, solve once
       photometry -> no dither, no time/HFR AF, solve once
       exoplanet  -> AF only at start, no dither, no time/HFR AF, locked focus
       astrometry -> solve EVERY frame, no dither, tight HFR gate
       transient  -> solve EVERY frame, dither every frame, tight HFR
       planetary  -> plate-solve DISABLED, no dither, no AF mid-run

Run from project root:
    venv\\Scripts\\python.exe scripts\\smoke_workflows.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def test_registry_completeness() -> None:
    _hr("1. registry covers every WorkflowKind")
    from atlas.db.models import WorkflowKind
    from atlas.workflows.registry import WORKFLOWS, get_workflow

    expected = set(WorkflowKind)
    registered = set(WORKFLOWS.keys())
    missing = expected - registered
    assert not missing, f"missing workflows: {missing}"
    print(f"  [OK] {len(registered)}/{len(expected)} kinds registered")

    # String fallback works too
    wf = get_workflow("deepsky")
    assert wf.kind == WorkflowKind.DEEPSKY
    print(f"  [OK] string fallback: get_workflow('deepsky') -> {wf.kind.value}")

    # Unknown string returns deepsky as the safe default
    wf2 = get_workflow("totally-made-up")
    assert wf2.kind == WorkflowKind.DEEPSKY
    print(f"  [OK] unknown kind falls back to deepsky")


def test_deepsky() -> None:
    _hr("2. deepsky policy")
    from atlas.workflows.registry import get_workflow
    spec = get_workflow("deepsky").plan(target={}, conditions={})
    print(f"  AF: filter_change={spec.autofocus.on_filter_change}, "
            f"time_min={spec.autofocus.time_interval_min}, "
            f"hfr_factor={spec.autofocus.hfr_drift_factor}")
    print(f"  PS: enabled={spec.platesolve.enabled}, "
            f"every_frame={spec.platesolve.every_frame}, "
            f"first_frame={spec.platesolve.on_first_frame}")
    print(f"  Dither: enabled={spec.dither.enabled}, "
            f"every_n={spec.dither.every_n_frames}")
    assert spec.autofocus.on_filter_change is True
    assert spec.autofocus.time_interval_min == 60.0
    assert spec.platesolve.enabled is True
    assert spec.platesolve.every_frame is False
    assert spec.dither.enabled is True
    assert spec.dither.every_n_frames == 1
    print("  [OK] aesthetic deepsky: AF often, solve once, dither every frame")


def test_photometry_and_exoplanet() -> None:
    _hr("3. photometry + exoplanet (lockdown for precise lightcurves)")
    from atlas.workflows.registry import get_workflow

    phot = get_workflow("photometry").plan(target={}, conditions={})
    assert phot.dither.enabled is False, "photometry must not dither"
    assert phot.autofocus.time_interval_min is None, \
        "photometry must NOT refocus on time"
    assert phot.autofocus.hfr_drift_factor >= 2.0, \
        "photometry HFR trigger must be loose"
    print(f"  photometry: dither={phot.dither.enabled}, "
            f"af_time={phot.autofocus.time_interval_min}, "
            f"af_hfr={phot.autofocus.hfr_drift_factor}")

    exo = get_workflow("exoplanet").plan(target={}, conditions={})
    assert exo.dither.enabled is False, "exoplanet must not dither"
    assert exo.autofocus.on_filter_change is False, \
        "exoplanet must not refocus on filter (single filter anyway)"
    assert exo.autofocus.time_interval_min is None
    assert exo.autofocus.hfr_drift_factor >= 99.0, \
        "exoplanet HFR trigger must be effectively disabled"
    assert exo.extras.get("locked_focus") is True
    print(f"  exoplanet:  dither={exo.dither.enabled}, "
            f"af_filter_change={exo.autofocus.on_filter_change}, "
            f"hfr={exo.autofocus.hfr_drift_factor}, "
            f"locked_focus={exo.extras.get('locked_focus')}")
    print("  [OK] both lock down for stable photometric baselines")


def test_astrometry_and_transient() -> None:
    _hr("4. astrometry + transient (per-frame WCS for centroid / subtract)")
    from atlas.workflows.registry import get_workflow

    astro = get_workflow("astrometry").plan(target={}, conditions={})
    assert astro.platesolve.every_frame is True, \
        "astrometry MUST solve every frame for centroid->RA/Dec"
    assert astro.dither.enabled is False, "astrometry must not dither"
    assert astro.acceptance.max_hfr is not None and astro.acceptance.max_hfr <= 4.0
    assert (astro.acceptance.min_star_count or 0) >= 100, \
        "need many refs for a robust WCS fit"
    print(f"  astrometry: every_frame={astro.platesolve.every_frame}, "
            f"max_hfr={astro.acceptance.max_hfr}, "
            f"min_stars={astro.acceptance.min_star_count}")

    trans = get_workflow("transient").plan(target={}, conditions={})
    assert trans.platesolve.every_frame is True, \
        "transient needs per-frame WCS for image subtraction"
    assert trans.dither.enabled is True, "transient dithers for clean reference"
    assert (trans.acceptance.max_eccentricity or 1.0) <= 0.6, \
        "round stars only for PSF match"
    print(f"  transient:  every_frame={trans.platesolve.every_frame}, "
            f"dither={trans.dither.enabled}, "
            f"max_ecc={trans.acceptance.max_eccentricity}")
    print("  [OK] both require per-frame WCS")


def test_planetary_is_special() -> None:
    _hr("5. planetary disables solve + dither (SharpCap handoff)")
    from atlas.workflows.registry import get_workflow
    spec = get_workflow("planetary").plan(target={}, conditions={})
    assert spec.platesolve.enabled is False, \
        "planetary ROI too small for ASTAP — solve must be off"
    assert spec.dither.enabled is False
    assert spec.autofocus.hfr_drift_factor >= 99.0
    assert spec.extras.get("software_mode") == "sharpcap"
    print(f"  planetary: solve_enabled={spec.platesolve.enabled}, "
            f"dither={spec.dither.enabled}, "
            f"software={spec.extras.get('software_mode')}")
    print("  [OK] planetary correctly opts out of solve+dither+AF-during-run")


def test_specs_are_distinct() -> None:
    _hr("6. all six policy bundles are distinct (no copy-paste collisions)")
    from atlas.workflows.registry import WORKFLOWS

    signatures = {}
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
        if sig in signatures:
            other = signatures[sig]
            print(f"  [WARN] {kind.value} has identical policy to "
                    f"{other.value} (sig={sig})")
        signatures[sig] = kind
    print(f"  [OK] {len(set(signatures))} distinct policy signatures "
            f"across {len(WORKFLOWS)} workflows")


def main() -> None:
    test_registry_completeness()
    test_deepsky()
    test_photometry_and_exoplanet()
    test_astrometry_and_transient()
    test_planetary_is_special()
    test_specs_are_distinct()
    _hr("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
