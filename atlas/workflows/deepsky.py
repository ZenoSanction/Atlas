"""Deep-sky imaging workflow — pretty pictures + calibrated photometric outputs.

This is the catch-all imaging workflow when no science priority applies.
Pipeline:
    1. Long subs in chosen filters with dithering
    2. Full calibration (bias/dark/flat)
    3. Stack via Siril (scriptable, free)
    4. Plate solve and embed WCS
    5. Optional photometric zero-point calibration against in-field standards

Policy choices:
  * Autofocus aggressively — long sessions, temp swings, filter changes
  * Plate-solve at sequence start + after meridian flip + after guiding
    recovery; not every frame (waste of time when guiding is steady)
  * Dither every frame — the gold standard for clean stacks
  * Acceptance gates: standard HFR / eccentricity, no SNR floor
"""
from __future__ import annotations

from atlas.db.models import WorkflowKind
from atlas.workflows.base import (
    AcceptancePolicy, AutofocusPolicy, DitherPolicy, PlateSolvePolicy,
    SequenceSpec, Workflow, WorkflowResult,
)


class DeepSkyWorkflow(Workflow):
    kind = WorkflowKind.DEEPSKY

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # Exposure plan: target-specific. Default is a single 60×180s L
        # block; real plans come from the Planner's exposure_plan
        # calculation (atlas/agents/planner.py).
        exposures = target.get("exposure_plan") or [
            {"filter": "L", "exposure_s": 180.0, "count": 60,
                "notes": "deepsky default"},
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
                hfr_drift_factor=1.25,
            ),
            platesolve=PlateSolvePolicy(
                on_first_frame=True,
                on_meridian_flip=True,
                on_guiding_recovery=True,
                every_frame=False,
                enabled=True,
            ),
            dither=DitherPolicy(
                enabled=True, every_n_frames=1,
                amount_px=5.0, settle_pixels=1.5,
                settle_time_s=10, settle_timeout_s=60,
            ),
            acceptance=AcceptancePolicy(
                max_hfr=4.0, max_eccentricity=0.65,
                max_background_adu=None, min_star_count=50,
                max_guide_rms_px=1.5,
            ),
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO bench day: Siril scriptable stacking
        return WorkflowResult(notes="TODO bench day: Siril stacking pipeline")
