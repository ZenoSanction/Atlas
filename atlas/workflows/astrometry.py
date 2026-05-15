"""Asteroid / comet astrometry workflow — Priority A.

Pipeline:
    1. Resolve MPC designation -> current ephemeris
    2. Compute non-sidereal tracking rates
    3. Capture short subs (typically 30-60 s) to avoid trailing
    4. Plate-solve each frame (ASTAP)
    5. Measure target centroid against Gaia DR3 reference stars
    6. Produce MPC-format report
    7. Queue submission to MPC (status=QUEUED)
"""
from __future__ import annotations

from atlas.db.models import WorkflowKind
from atlas.workflows.base import AutofocusPolicy, SequenceSpec, Workflow, WorkflowResult


class AstrometryWorkflow(Workflow):
    kind = WorkflowKind.ASTROMETRY

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # TODO Phase 2:
        # - Use astroquery/MPC to fetch ephemeris for ``target["mpc_designation"]``
        # - Compute non-sidereal rates (dRA/dt, dDec/dt) at observation time
        # - Pick exposure that keeps trailing < 1 px
        # - Build SequenceSpec with non_sidereal_rates populated
        # Astrometry: focus quality directly limits centroid precision.
        # Autofocus once before short series; series too short to need rerun.
        return SequenceSpec(
            target_name=target.get("name", "?"),
            workflow=self.kind,
            exposures=[{"filter": "L", "exposure_s": 30.0, "count": 5,
                         "dither": False, "notes": "TODO Phase 2"}],
            dither=False,
            autofocus=AutofocusPolicy(
                before_sequence=True, on_filter_change=False,
                temperature_delta_c=2.0, time_interval_min=None,
                hfr_drift_pct=20.0,
            ),
            non_sidereal_rates={"d_ra_arcsec_per_min": 0.0,
                                 "d_dec_arcsec_per_min": 0.0,
                                 "TODO": "Phase 2"},
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO Phase 2:
        # - Plate-solve each frame
        # - Centroid the moving object on each frame
        # - Compute astrometric position with Gaia DR3 reference
        # - Build MPC 1992-format observation lines
        # - Create Measurement rows (kind=astrometry)
        # - Queue Submission(destination=MPC, status=QUEUED)
        return WorkflowResult(notes="TODO Phase 2: astrometry pipeline")
