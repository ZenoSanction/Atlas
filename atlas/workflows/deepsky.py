"""Deep-sky imaging workflow — pretty pictures + calibrated photometric outputs.

This is the catch-all imaging workflow when no science priority applies.
Pipeline:
    1. Long subs in chosen filters with dithering
    2. Full calibration (bias/dark/flat)
    3. Stack via Siril (scriptable, free)
    4. Plate solve and embed WCS
    5. Optional photometric zero-point calibration against in-field standards
"""
from __future__ import annotations

from atlas.db.models import WorkflowKind
from atlas.workflows.base import AutofocusPolicy, SequenceSpec, Workflow, WorkflowResult


class DeepSkyWorkflow(Workflow):
    kind = WorkflowKind.DEEPSKY

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # TODO Phase 2: filter rotation, autofocus cadence, exposure plan
        # from equipment profile + target brightness
        # Deep-sky aesthetic: autofocus before sequence, on filter change,
        # and at a reasonable temperature/time cadence during long nights.
        return SequenceSpec(
            target_name=target.get("name", "?"),
            workflow=self.kind,
            exposures=[{"filter": "L", "exposure_s": 180.0, "count": 60,
                         "dither": True, "notes": "TODO Phase 2"}],
            dither=True,
            autofocus=AutofocusPolicy(
                before_sequence=True, on_filter_change=True,
                temperature_delta_c=2.0, time_interval_min=60,
                hfr_drift_pct=15.0,
            ),
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO Phase 2: Siril scriptable stacking
        return WorkflowResult(notes="TODO Phase 2: Siril stacking pipeline")
