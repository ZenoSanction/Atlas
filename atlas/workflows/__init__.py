"""Science workflow pipelines.

A workflow defines the stages from target → captured frames → measurements →
submission for one kind of science work. Workflows are declarative: adding a
new kind of science means defining its stages, not modifying the agents.

Priority order from Rounds 1–4:
    A.  Asteroid/comet astrometry      (workflows.astrometry)
    B.  Variable star + exoplanet      (workflows.photometry)
    C1. Transient / supernova hunting  (workflows.transient)
    C2. Planetary imaging              (workflows.planetary)
        Deep-sky aesthetic             (workflows.deepsky)
"""
from atlas.workflows.base import (
    AutofocusPolicy, SequenceSpec, Workflow, WorkflowResult,
)

__all__ = ["Workflow", "WorkflowResult", "SequenceSpec", "AutofocusPolicy"]
