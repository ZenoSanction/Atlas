"""Asteroid / comet astrometry workflow — MPC submission target.

Pipeline:
    1. Resolve MPC designation -> current ephemeris
    2. Compute non-sidereal tracking rates
    3. Capture short subs (typically 30-60 s) to avoid trailing
    4. Plate-solve EVERY frame (ASTAP)
    5. Measure target centroid against Gaia DR3 reference stars
    6. Produce MPC-format report
    7. Queue submission to MPC (status=QUEUED)

Policy choices:
  * Autofocus once before the series; the series is too short for
    temp drift to matter. HFR-drift trigger stays armed in case the
    very first frame catches focus already off.
  * Plate-solve EVERY frame. Astrometry is literally the act of
    measuring where a moving object is against a known star field.
    Every frame needs its own WCS for the centroid -> RA/Dec
    conversion to be valid.
  * NO dither. The point of the series is to measure position over
    time; an artificial pointing shift between frames would have to
    be subtracted out and is just an error source.
  * Acceptance: tight FWHM (centroid precision = FWHM / sqrt(SNR));
    high min-star-count (need plenty of reference stars for a good
    WCS solution).
"""
from __future__ import annotations

from atlas.db.models import WorkflowKind
from atlas.workflows.base import (
    AcceptancePolicy, AutofocusPolicy, DitherPolicy, PlateSolvePolicy,
    SequenceSpec, Workflow, WorkflowResult,
)


class AstrometryWorkflow(Workflow):
    kind = WorkflowKind.ASTROMETRY

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # Short subs keep the moving object's trail within 1 px even
        # at fast solar-system rates. Planner can override via
        # exposure_plan; default is 5×30s.
        exposures = target.get("exposure_plan") or [
            {"filter": "L", "exposure_s": 30.0, "count": 5,
                "notes": "astrometry default — short subs"},
        ]
        return SequenceSpec(
            target_name=target.get("target_name") or target.get("name", "?"),
            workflow=self.kind,
            exposures=exposures,
            autofocus=AutofocusPolicy(
                before_sequence=True,
                on_filter_change=False,    # almost always single filter
                temperature_delta_c=2.0,
                time_interval_min=None,    # series too short
                hfr_drift_factor=1.30,     # mild — only if it's actively bad
            ),
            platesolve=PlateSolvePolicy(
                on_first_frame=True,
                on_meridian_flip=True,
                on_guiding_recovery=True,
                every_frame=True,         # critical for centroid -> RA/Dec
                enabled=True,
            ),
            dither=DitherPolicy(enabled=False, every_n_frames=999),
            acceptance=AcceptancePolicy(
                max_hfr=3.5,               # tight — centroid precision
                max_eccentricity=0.60,
                max_background_adu=None,
                min_star_count=100,        # plenty of refs for WCS fit
                max_guide_rms_px=1.0,
            ),
            non_sidereal_rates=target.get("non_sidereal_rates")
                or {"d_ra_arcsec_per_min": 0.0,
                     "d_dec_arcsec_per_min": 0.0,
                     "TODO": "MPC ephemeris fetch on bench day"},
            extras={"requires_per_frame_wcs": True},
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO bench day:
        # - Plate-solve each frame
        # - Centroid the moving object on each frame
        # - Compute astrometric position with Gaia DR3 reference
        # - Build MPC 1992-format observation lines
        # - Measurement rows (kind=astrometry)
        # - Queue Submission(destination=MPC, status=QUEUED)
        return WorkflowResult(notes="TODO bench day: astrometry pipeline")
