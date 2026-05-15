"""Planetary imaging workflow — Priority C2.

SharpCap-based lucky imaging: high frame rate, ROI crop, SER video,
post-process via AutoStakkert!4 + RegiStax/WinJUPOS sharpening.

ATLAS does not run SharpCap directly during this Phase 1 build; the
Operator agent will launch it as an external process and poll for ready
state (per the multi-agent design's software orchestration rule).
"""
from __future__ import annotations

from atlas.db.models import WorkflowKind
from atlas.workflows.base import AutofocusPolicy, SequenceSpec, Workflow, WorkflowResult


class PlanetaryWorkflow(Workflow):
    kind = WorkflowKind.PLANETARY

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # TODO Phase 2: SharpCap launch + ROI / gain / fps setup
        # Planetary: autofocus is delicate — manual or NINA's coarse pass,
        # then handoff to SharpCap which has its own focus aids.
        return SequenceSpec(
            target_name=target.get("name", "?"),
            workflow=self.kind,
            exposures=[{"filter": "L", "exposure_s": 0.008, "count": 20000,
                         "dither": False, "notes": "TODO Phase 2: SharpCap SER"}],
            dither=False,
            autofocus=AutofocusPolicy(
                before_sequence=True, on_filter_change=False,
                temperature_delta_c=99.0, time_interval_min=None,
                hfr_drift_pct=None,
            ),
            extras={"software_mode": "sharpcap"},
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO Phase 2: AutoStakkert!4 CLI + post-sharpen
        return WorkflowResult(notes="TODO Phase 2: planetary lucky-imaging pipeline")
