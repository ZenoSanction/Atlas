"""Variable star + exoplanet transit photometry workflow — Priority B.

Pipeline (variable star):
    1. Pull AAVSO comp star sequence for the target
    2. Capture series in V (or Sloan-r) with no dithering
    3. Aperture (or PSF) photometry against comp/check stars
    4. Build AAVSO observation record(s)
    5. Queue submission to AAVSO

Pipeline (exoplanet transit):
    Same as above, but fixed field, longer continuous series spanning the
    transit window, light curve fit, NASA Exoplanet Watch format output.
"""
from __future__ import annotations

from atlas.db.models import WorkflowKind
from atlas.workflows.base import AutofocusPolicy, SequenceSpec, Workflow, WorkflowResult


class PhotometryWorkflow(Workflow):
    kind = WorkflowKind.PHOTOMETRY

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # TODO Phase 2:
        # - Determine filter from target metadata (default V for variable star)
        # - Compute appropriate exposure to put target at ~70% well depth
        # - Long continuous series, no dither
        # Photometry: autofocus on entry and on filter change, but NOT
        # mid-series — focus changes break the differential photometry baseline.
        return SequenceSpec(
            target_name=target.get("name", "?"),
            workflow=self.kind,
            exposures=[{"filter": "V", "exposure_s": 60.0, "count": 60,
                         "dither": False, "notes": "TODO Phase 2"}],
            dither=False,
            autofocus=AutofocusPolicy(
                before_sequence=True, on_filter_change=True,
                temperature_delta_c=3.0, time_interval_min=None,
                hfr_drift_pct=None,
            ),
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO Phase 2:
        # - Calibrate frames (bias/dark/flat)
        # - Plate-solve
        # - Aperture photometry against AAVSO comp stars
        # - Compute differential mag + uncertainty
        # - Create Measurement rows (kind=photometry)
        # - Queue Submission(destination=AAVSO, status=QUEUED)
        return WorkflowResult(notes="TODO Phase 2: photometry pipeline")


class ExoplanetWorkflow(Workflow):
    kind = WorkflowKind.EXOPLANET

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # TODO Phase 2: schedule across the predicted transit window
        # Exoplanet transit: NO autofocus mid-sequence. Locking focus is
        # critical to avoid systematic flux changes during the transit.
        return SequenceSpec(
            target_name=target.get("name", "?"),
            workflow=self.kind,
            exposures=[{"filter": "Rc", "exposure_s": 60.0, "count": 240,
                         "dither": False, "notes": "TODO Phase 2"}],
            dither=False,
            autofocus=AutofocusPolicy(
                before_sequence=True, on_filter_change=False,
                temperature_delta_c=99.0, time_interval_min=None,
                hfr_drift_pct=None,
            ),
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO Phase 2:
        # - Differential photometry across the series
        # - Fit transit model (PyTransit / batman)
        # - Produce light curve in NASA Exoplanet Watch / AAVSO Exoplanet format
        return WorkflowResult(notes="TODO Phase 2: exoplanet pipeline")
