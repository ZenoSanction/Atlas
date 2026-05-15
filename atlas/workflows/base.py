"""Workflow base class.

A Workflow has four stages:

    plan(target, conditions) -> SequenceSpec     # what to capture
    acquire(spec) -> list[FrameId]               # commanded via NINA
    process(frame_ids) -> list[MeasurementId]    # Archivist pipeline
    submit(measurement_ids) -> list[SubmissionId]  # queue for human approval

Subclasses override the stages they need to customise. Most subclasses
override ``plan()`` and ``process()`` heavily; ``acquire()`` is mostly shared.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from atlas.db.models import WorkflowKind


@dataclass
class AutofocusPolicy:
    """When the ZWO EAF (or any NINA-driven focuser) should run autofocus.

    Autofocus is on the critical path for every science workflow. Stars out
    of focus mean inflated FWHM, weaker photometry, worse astrometry, and
    missed faint sources in transient subtraction. The Critic also fires
    a focus_drift alert if HFR climbs out of band during a sequence.
    """
    before_sequence: bool = True
    on_filter_change: bool = True
    temperature_delta_c: float = 2.0   # rerun if focuser temp moves this much
    time_interval_min: int | None = 60  # rerun every N minutes; None disables
    hfr_drift_pct: float | None = 15.0  # rerun if HFR climbs this %


@dataclass
class SequenceSpec:
    """A workflow-built capture plan ready to hand to NINA."""
    target_name: str
    workflow: WorkflowKind
    exposures: list[dict] = field(default_factory=list)
    # each exposure: { filter, exposure_s, count, dither (bool), notes }
    dither: bool = False
    autofocus: AutofocusPolicy = field(default_factory=AutofocusPolicy)
    non_sidereal_rates: dict | None = None  # for asteroid/comet
    extras: dict = field(default_factory=dict)


@dataclass
class WorkflowResult:
    measurement_ids: list[int] = field(default_factory=list)
    submission_ids: list[int] = field(default_factory=list)
    stack_product_ids: list[int] = field(default_factory=list)
    notes: str | None = None


class Workflow(ABC):
    """ABC for all science workflows."""

    kind: WorkflowKind  # subclass-set

    @abstractmethod
    def plan(self, *, target: dict, conditions: dict) -> SequenceSpec:
        """Build a capture sequence for the target under given conditions."""

    @abstractmethod
    def process(self, *, frame_ids: list[int], session_id: int) -> WorkflowResult:
        """Process captured frames into measurements and queued submissions."""
