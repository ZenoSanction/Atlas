"""Supernova / transient hunting — Priority C1.

Pipeline:
    1. Visit field. If <3 prior visits, accumulate to reference frame and stop.
    2. From visit 3 onward: plate-solve and image-subtract against reference.
    3. Source-extract residuals; filter on SNR, FWHM, ellipticity.
    4. Catalog cross-match against Gaia DR3, Pan-STARRS, MPC.
    5. Survivors -> Measurement(kind=transient_candidate).
    6. Queue Submission(destination=TNS, status=QUEUED).
"""
from __future__ import annotations

from atlas.db.models import WorkflowKind
from atlas.workflows.base import AutofocusPolicy, SequenceSpec, Workflow, WorkflowResult


class TransientWorkflow(Workflow):
    kind = WorkflowKind.TRANSIENT

    MIN_VISITS_FOR_REFERENCE = 3

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # TODO Phase 2:
        # - Determine field key from target coords
        # - Look up prior visit count; if < MIN_VISITS, this visit is reference build
        # - Pick exposure depth to reach desired limiting magnitude
        # Transient hunting: PSF stability matters for image subtraction.
        # Autofocus before sequence and again if HFR drifts.
        return SequenceSpec(
            target_name=target.get("name", "?"),
            workflow=self.kind,
            exposures=[{"filter": "L", "exposure_s": 120.0, "count": 10,
                         "dither": True, "notes": "TODO Phase 2"}],
            dither=True,
            autofocus=AutofocusPolicy(
                before_sequence=True, on_filter_change=True,
                temperature_delta_c=2.0, time_interval_min=60,
                hfr_drift_pct=15.0,
            ),
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO Phase 2: image subtraction (HOTPANTS or PyZOGY), source extraction,
        # cross-match, queue confirmed candidates to TNS.
        return WorkflowResult(notes="TODO Phase 2: transient detection pipeline")
