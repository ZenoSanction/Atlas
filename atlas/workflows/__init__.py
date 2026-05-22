"""Science workflow pipelines.

A workflow defines the stages from target -> captured frames -> measurements ->
submission for one kind of science work. Workflows are declarative: adding a
new kind of science means defining its stages, not modifying the agents.

Priority order (from the multi-agent design rounds):
    A.  Asteroid/comet astrometry      (workflows.astrometry)
    B.  Variable star + exoplanet      (workflows.photometry)
    C1. Transient / supernova hunting  (workflows.transient)
    C2. Planetary imaging              (workflows.planetary)
        Deep-sky aesthetic             (workflows.deepsky)

Each workflow exposes four policy slots the orchestration layer reads:
    AutofocusPolicy   — when to refocus
    PlateSolvePolicy  — when to solve+sync the mount
    DitherPolicy      — whether/when to dither
    AcceptancePolicy  — per-frame quality gates

These are the differences that matter at the orchestrator level
between, say, exoplanet (locked focus, no dither, lock pointing once)
and astrometry (per-frame solve, no dither, tight HFR gate).
"""
from atlas.workflows.base import (
    AcceptancePolicy, AutofocusPolicy, DitherPolicy, PlateSolvePolicy,
    SequenceSpec, Workflow, WorkflowResult,
)
from atlas.workflows.registry import (
    WORKFLOWS, get_workflow, list_workflows,
)

__all__ = [
    "Workflow", "WorkflowResult", "SequenceSpec",
    "AutofocusPolicy", "PlateSolvePolicy", "DitherPolicy", "AcceptancePolicy",
    "WORKFLOWS", "get_workflow", "list_workflows",
]
