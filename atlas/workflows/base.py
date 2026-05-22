"""Workflow base class.

A Workflow has four stages:

    plan(target, conditions) -> SequenceSpec     # what to capture
    acquire(spec) -> list[FrameId]               # commanded via NINA
    process(frame_ids) -> list[MeasurementId]    # Archivist pipeline
    submit(measurement_ids) -> list[SubmissionId]  # queue for human approval

Each workflow declares *policies* that drive the capture orchestration:

  * AutofocusPolicy   — when to refocus
  * PlateSolvePolicy  — when to solve+sync the mount
  * DitherPolicy      — whether/when to dither between frames
  * AcceptancePolicy  — per-frame quality gates (Critic + Archivist)

These policies are the per-workflow differences that matter for the
agents downstream. Deep-sky and exoplanet can use the same hardware
but they need *very* different policies — deep-sky wants frequent
refocus + dither, exoplanet wants locked focus + no dither (any
focus change or dither during a transit window destroys the
photometric baseline).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.db.models import WorkflowKind


# ---- policy dataclasses --------------------------------------------------

@dataclass
class AutofocusPolicy:
    """When the ZWO EAF (or any NINA-driven focuser) should run autofocus.

    Autofocus is on the critical path for every science workflow. Stars out
    of focus mean inflated FWHM, weaker photometry, worse astrometry, and
    missed faint sources in transient subtraction. The Critic also fires
    a focus_drift alert if HFR climbs out of band during a sequence.

    Maps 1:1 onto AutofocusDecisionEngine kwargs — Operator constructs
    the engine directly from this policy.
    """
    before_sequence: bool = True            # session-start trigger
    on_filter_change: bool = True           # mono-setup trigger
    temperature_delta_c: float = 2.0        # rerun if focuser temp drifts
    time_interval_min: Optional[float] = 60.0  # rerun every N min; None disables
    hfr_drift_factor: float = 1.25          # rerun if HFR > REF * factor


@dataclass
class PlateSolvePolicy:
    """When to plate-solve + sync the mount.

    Wired into CaptureSequence via the plate-solve orchestrator. Each
    workflow tunes this differently:

      Deep-sky: solve once before first frame, after meridian flips,
                after guiding recoveries.
      Photometry / Exoplanet: solve once at start; the field must not
                              move once data collection begins.
      Transient / Astrometry: solve EVERY frame (the WCS goes into
                              measurements and submissions).
      Planetary: never — the ROI is too small for a meaningful solve.
    """
    on_first_frame: bool = True             # always sync before first real frame
    on_meridian_flip: bool = True
    on_guiding_recovery: bool = True
    every_frame: bool = False               # astrometry/transient need this
    enabled: bool = True                    # planetary turns the whole thing off
    first_pass_radius_deg: float = 5.0
    retry_radius_deg: float = 15.0


@dataclass
class DitherPolicy:
    """Between-frame dither cadence.

    Dithering shifts the field by a few pixels between frames so hot
    pixels and CCD defects don't stack consistently. Aesthetic /
    transient work benefits; photometry / exoplanet work is harmed by
    it (sub-pixel reference movement breaks differential baselines).
    """
    enabled: bool = True
    every_n_frames: int = 1                 # 1 = every frame
    amount_px: float = 5.0
    settle_pixels: float = 1.5
    settle_time_s: int = 10
    settle_timeout_s: int = 60


@dataclass
class AcceptancePolicy:
    """Per-frame quality gates the Archivist uses to grade incoming
    frames. Each metric is a *maximum acceptable value*; frames over
    threshold are graded C/D and excluded from the night's stack.

    Set a threshold to None to disable that check.
    """
    max_hfr: Optional[float] = 4.0          # arcsec or pixels (NINA reports px)
    max_eccentricity: Optional[float] = 0.65
    max_background_adu: Optional[float] = None    # cloud / moon glare
    min_star_count: Optional[int] = 50           # cloud / dew
    max_guide_rms_px: Optional[float] = 1.5      # tracking-error gate


# ---- the full sequence spec ----------------------------------------------

@dataclass
class SequenceSpec:
    """A workflow-built capture plan ready to hand to the Operator.

    The exposure list is the "what to capture" answer. The policies
    are the "how the orchestrator should behave during capture"
    answer. Everything CaptureSequence + downstream agents need to
    correctly run a science workflow lives here."""
    target_name: str
    workflow: WorkflowKind
    exposures: list[dict] = field(default_factory=list)
    # each exposure: { filter, exposure_s, count, notes }
    autofocus: AutofocusPolicy = field(default_factory=AutofocusPolicy)
    platesolve: PlateSolvePolicy = field(default_factory=PlateSolvePolicy)
    dither: DitherPolicy = field(default_factory=DitherPolicy)
    acceptance: AcceptancePolicy = field(default_factory=AcceptancePolicy)
    non_sidereal_rates: Optional[dict] = None     # asteroid/comet
    extras: dict = field(default_factory=dict)

    # Total exposure-time in this spec (for ETA, scheduler hints)
    @property
    def total_exposure_s(self) -> float:
        return sum(float(e.get("exposure_s") or 0)
                     * int(e.get("count") or 0)
                     for e in self.exposures)


@dataclass
class WorkflowResult:
    measurement_ids: list[int] = field(default_factory=list)
    submission_ids: list[int] = field(default_factory=list)
    stack_product_ids: list[int] = field(default_factory=list)
    notes: Optional[str] = None


class Workflow(ABC):
    """ABC for all science workflows."""

    kind: WorkflowKind  # subclass-set

    @abstractmethod
    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        """Build a capture sequence for the target under given conditions."""

    @abstractmethod
    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        """Process captured frames into measurements and queued submissions."""

    # Convenience accessors so callers (Operator, Planner) can pull a
    # policy out without first calling plan(). Defaults to whatever
    # plan() would emit for an empty target — letting the agents
    # decide policy independent of any specific target.
    def default_autofocus(self) -> AutofocusPolicy:
        return self.plan(target={}, conditions={}).autofocus

    def default_platesolve(self) -> PlateSolvePolicy:
        return self.plan(target={}, conditions={}).platesolve

    def default_dither(self) -> DitherPolicy:
        return self.plan(target={}, conditions={}).dither

    def default_acceptance(self) -> AcceptancePolicy:
        return self.plan(target={}, conditions={}).acceptance
