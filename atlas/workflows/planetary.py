"""Planetary imaging workflow — SharpCap-based lucky imaging.

SharpCap-based lucky imaging: high frame rate, ROI crop, SER video,
post-process via AutoStakkert!4 + RegiStax/WinJUPOS sharpening.

ATLAS does not run SharpCap directly during this Phase 2 build; the
Operator agent will launch it as an external process and poll for ready
state.

Policy choices (everything here is fundamentally different from
deep-sky / photometry workflows):
  * Autofocus before capture, then NEVER touch focus mid-run — SharpCap
    has its own focus-aid loop the human uses on Mars/Jupiter/Saturn.
  * Plate-solve DISABLED. The ROI is too tiny (a few hundred px) for
    ASTAP to find enough stars. The mount stays where the human pointed
    it.
  * Dither DISABLED. The whole point of lucky imaging is that the same
    pixel column samples the same atmospheric column over thousands of
    frames — moving the field defeats the technique.
  * Acceptance: doesn't apply at the FITS-grade level; downstream
    AutoStakkert! picks the best N% of frames in the SER itself.
"""
from __future__ import annotations

from atlas.db.models import WorkflowKind
from atlas.workflows.base import (
    AcceptancePolicy, AutofocusPolicy, DitherPolicy, PlateSolvePolicy,
    SequenceSpec, Workflow, WorkflowResult,
)


class PlanetaryWorkflow(Workflow):
    kind = WorkflowKind.PLANETARY

    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        # Planetary is fundamentally a SharpCap handoff. The "exposures"
        # entry here is for record-keeping; the actual SER stream is
        # controlled by SharpCap with our extras hints.
        exposures = target.get("exposure_plan") or [
            {"filter": "L", "exposure_s": 0.008, "count": 20000,
                "notes": "SharpCap SER capture — actual fps/exp set in SharpCap"},
        ]
        return SequenceSpec(
            target_name=target.get("target_name") or target.get("name", "?"),
            workflow=self.kind,
            exposures=exposures,
            autofocus=AutofocusPolicy(
                before_sequence=True,
                on_filter_change=False,
                temperature_delta_c=99.0,   # never
                time_interval_min=None,
                hfr_drift_factor=99.0,      # never
            ),
            platesolve=PlateSolvePolicy(
                enabled=False,              # disabled — ROI too small
                on_first_frame=False,
                on_meridian_flip=False,
                on_guiding_recovery=False,
                every_frame=False,
            ),
            dither=DitherPolicy(enabled=False, every_n_frames=999),
            acceptance=AcceptancePolicy(
                max_hfr=None, max_eccentricity=None,
                max_background_adu=None,
                min_star_count=None,        # planetary frame has 1 "star"
                max_guide_rms_px=None,      # no guider on planetary
            ),
            extras={
                "software_mode": "sharpcap",
                "capture_format": "ser",
                "post_process": ["autostakkert4", "registax"],
            },
        )

    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        # TODO bench day: AutoStakkert!4 CLI + post-sharpen
        return WorkflowResult(notes="TODO bench day: planetary lucky-imaging pipeline")
