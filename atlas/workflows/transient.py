"""Supernova / transient hunting workflow.

Pipeline:
    1. Visit field. If <3 prior visits, accumulate to reference frame and stop.
    2. From visit 3 onward: plate-solve and image-subtract against reference.
    3. Source-extract residuals; filter on SNR, FWHM, ellipticity.
    4. Catalog cross-match against Gaia DR3, Pan-STARRS, MPC.
    5. Survivors -> Measurement(kind=transient_candidate).
    6. Queue Submission(destination=TNS, status=QUEUED).

Policy choices:
  * Autofocus aggressively before sequence + on HFR drift. PSF
    stability is critical for image subtraction — a different PSF
    on the new frame vs reference produces "residuals" that are
    actually just convolution-mismatch noise.
  * Plate-solve EVERY frame. The subtraction needs a per-frame WCS
    for alignment to the reference.
  * Dither between frames. We're co-adding multiple visits into the
    reference; dithering kills hot-pixel correlation in the stack.
  * Acceptance: tight FWHM + eccentricity (PSF mismatch is fatal),
    high star count (need enough refs for WCS + PSF match).
"""
from __future__ import annotations

from atlas.db.models import WorkflowKind
from atlas.workflows.base import (
    AcceptancePolicy, AutofocusPolicy, DitherPolicy, PlateSolvePolicy,
    SequenceSpec, Workflow, WorkflowResult,
)


class TransientWorkflow(Workflow):
    kind = WorkflowKind.TRANSIENT

    MIN_VISITS_FOR_REFERENCE = 3

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # Default: 10×120s. Planner can override via exposure_plan
        # based on the field's limiting-magnitude budget.
        exposures = target.get("exposure_plan") or [
            {"filter": "L", "exposure_s": 120.0, "count": 10,
                "notes": "transient default — co-add for depth"},
        ]
        return SequenceSpec(
            target_name=target.get("target_name") or target.get("name", "?"),
            workflow=self.kind,
            exposures=exposures,
            autofocus=AutofocusPolicy(
                before_sequence=True,
                on_filter_change=True,
                temperature_delta_c=2.0,
                time_interval_min=60.0,
                hfr_drift_factor=1.20,    # tight — PSF stability critical
            ),
            platesolve=PlateSolvePolicy(
                on_first_frame=True,
                on_meridian_flip=True,
                on_guiding_recovery=True,
                every_frame=True,          # critical for subtraction alignment
                enabled=True,
            ),
            dither=DitherPolicy(
                enabled=True, every_n_frames=1,
                amount_px=4.0, settle_pixels=1.5,
                settle_time_s=10, settle_timeout_s=60,
            ),
            acceptance=AcceptancePolicy(
                max_hfr=3.5,               # PSF mismatch -> false candidates
                max_eccentricity=0.55,     # round stars only
                max_background_adu=None,
                min_star_count=80,         # need refs for WCS + PSF model
                max_guide_rms_px=1.2,
            ),
            extras={"min_visits_for_reference": self.MIN_VISITS_FOR_REFERENCE,
                     "requires_per_frame_wcs": True},
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO bench day: image subtraction (HOTPANTS or PyZOGY), source extraction,
        # cross-match, queue confirmed candidates to TNS.
        return WorkflowResult(notes="TODO bench day: transient detection pipeline")
