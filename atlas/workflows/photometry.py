"""Variable star + exoplanet transit photometry workflows.

Pipeline (variable star):
    1. Pull AAVSO comp star sequence for the target
    2. Capture series in V (or Sloan-r) with no dithering
    3. Aperture (or PSF) photometry against comp/check stars
    4. Build AAVSO observation record(s)
    5. Queue submission to AAVSO

Pipeline (exoplanet transit):
    Same shape but fixed field, much longer continuous series spanning
    the transit window, light curve fit, NASA Exoplanet Watch format.

Policy choices for BOTH photometry and exoplanet:
  * Autofocus before the sequence only (or on filter change for
    photometry). Mid-series refocus shifts the PSF and breaks the
    differential baseline — particularly fatal for exoplanet transits.
  * Plate-solve once at the start to lock pointing; not every frame.
    Once we have a WCS, every frame stays on the same field.
  * NO dither. Dithering changes which CCD pixels see the target +
    comp stars, which forces flat-field interpolation noise into the
    differential signal. Bad for precise photometry.
  * Acceptance: tight SNR gate (need consistent measurements);
    loose HFR (FWHM can vary by seeing — what matters is *consistency*,
    not absolute sharpness).
"""
from __future__ import annotations

from atlas.db.models import WorkflowKind
from atlas.workflows.base import (
    AcceptancePolicy, AutofocusPolicy, DitherPolicy, PlateSolvePolicy,
    SequenceSpec, Workflow, WorkflowResult,
)


class PhotometryWorkflow(Workflow):
    """Variable-star (AAVSO) photometry."""
    kind = WorkflowKind.PHOTOMETRY

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # Filter from target metadata, default V (AAVSO standard band)
        filt = (target.get("filter") or "V").upper()
        exposure_s = float(target.get("exposure_s") or 60.0)
        count = int(target.get("count") or 60)
        exposures = target.get("exposure_plan") or [
            {"filter": filt, "exposure_s": exposure_s, "count": count,
                "notes": "AAVSO photometry"},
        ]
        return SequenceSpec(
            target_name=target.get("target_name") or target.get("name", "?"),
            workflow=self.kind,
            exposures=exposures,
            autofocus=AutofocusPolicy(
                before_sequence=True,
                on_filter_change=True,    # mono setups; OSC ignores
                temperature_delta_c=3.0,   # looser — drift = systematic, refocus = systematic-step
                time_interval_min=None,    # NO time-based reruns during series
                hfr_drift_factor=2.0,      # very loose — only refire on real drift
            ),
            platesolve=PlateSolvePolicy(
                on_first_frame=True,
                on_meridian_flip=True,    # still need to re-anchor after flip
                on_guiding_recovery=True,
                every_frame=False,
                enabled=True,
            ),
            dither=DitherPolicy(enabled=False, every_n_frames=999),
            acceptance=AcceptancePolicy(
                max_hfr=5.0,               # loose — consistency matters more
                max_eccentricity=0.70,
                max_background_adu=None,   # photometric pipeline does its own bg
                min_star_count=30,         # need comp stars detectable
                max_guide_rms_px=1.0,      # tight — drift = aperture noise
            ),
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO bench day:
        # - Calibrate frames (bias/dark/flat)
        # - Plate-solve (every frame uses the locked WCS from first)
        # - Aperture photometry vs AAVSO comp stars
        # - Compute differential mag + uncertainty
        # - Measurement rows (kind=photometry)
        # - Queue Submission(destination=AAVSO, status=QUEUED)
        return WorkflowResult(notes="TODO bench day: photometry pipeline")


class ExoplanetWorkflow(Workflow):
    """Exoplanet transit observation (NASA Exoplanet Watch / AAVSO Exoplanet).

    Even stricter than variable-star photometry: any focus change or
    dither during the transit window destroys the lightcurve. We
    lock everything down for the duration of the series.
    """
    kind = WorkflowKind.EXOPLANET

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        filt = (target.get("filter") or "Rc").upper()
        # Long continuous series across the transit window. Caller
        # (Planner) supplies a count that covers ingress + transit + egress
        # with margin.
        exposure_s = float(target.get("exposure_s") or 60.0)
        count = int(target.get("count") or 240)
        exposures = target.get("exposure_plan") or [
            {"filter": filt, "exposure_s": exposure_s, "count": count,
                "notes": "exoplanet transit"},
        ]
        return SequenceSpec(
            target_name=target.get("target_name") or target.get("name", "?"),
            workflow=self.kind,
            exposures=exposures,
            autofocus=AutofocusPolicy(
                before_sequence=True,
                on_filter_change=False,    # single filter, never changes
                temperature_delta_c=99.0,  # effectively never refocus on temp
                time_interval_min=None,    # never refocus on time
                hfr_drift_factor=99.0,     # never refocus on HFR
            ),
            platesolve=PlateSolvePolicy(
                on_first_frame=True,       # lock pointing once
                on_meridian_flip=True,     # cannot avoid re-anchor at flip
                on_guiding_recovery=True,  # if guiding drops, must re-anchor
                every_frame=False,
                enabled=True,
            ),
            dither=DitherPolicy(enabled=False, every_n_frames=999),
            acceptance=AcceptancePolicy(
                max_hfr=6.0,
                max_eccentricity=0.70,
                max_background_adu=None,
                min_star_count=30,
                max_guide_rms_px=0.8,     # very tight — transit signal is mmag
            ),
            extras={"locked_focus": True,
                     "transit_window_required": True},
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO bench day:
        # - Differential photometry across the series
        # - Fit transit model (PyTransit / batman)
        # - Produce lightcurve in NASA Exoplanet Watch / AAVSO Exoplanet format
        return WorkflowResult(notes="TODO bench day: exoplanet pipeline")
