"""Workflow registry — single source of truth for kind -> class lookup.

Agents (Planner, Operator) ask the registry which workflow to use
for a target's WorkflowKind. New workflows are added by importing
the class and adding an entry below — no other code changes needed.

Usage:
    from atlas.workflows.registry import get_workflow, list_workflows

    wf = get_workflow(target.workflow_kind)
    spec = wf.plan(target=target_dict, conditions=tonight_dict)

The registry is import-time only — no DB lookups, no IO. That keeps
the planning path fast (a session with 4 targets calls get_workflow
4+ times) and makes "what workflows exist" easy to audit.
"""
from __future__ import annotations

from typing import Type

from atlas.db.models import WorkflowKind
from atlas.workflows.base import Workflow
from atlas.workflows.astrometry import AstrometryWorkflow
from atlas.workflows.deepsky import DeepSkyWorkflow
from atlas.workflows.photometry import ExoplanetWorkflow, PhotometryWorkflow
from atlas.workflows.planetary import PlanetaryWorkflow
from atlas.workflows.transient import TransientWorkflow


# Single import-time table. Add a new workflow by adding one line.
WORKFLOWS: dict[WorkflowKind, Type[Workflow]] = {
    WorkflowKind.DEEPSKY:    DeepSkyWorkflow,
    WorkflowKind.PHOTOMETRY: PhotometryWorkflow,
    WorkflowKind.EXOPLANET:  ExoplanetWorkflow,
    WorkflowKind.TRANSIENT:  TransientWorkflow,
    WorkflowKind.PLANETARY:  PlanetaryWorkflow,
    WorkflowKind.ASTROMETRY: AstrometryWorkflow,
}


def get_workflow(kind: WorkflowKind | str) -> Workflow:
    """Instantiate the workflow class for a given kind.

    Accepts either the enum or its string value (Planner sometimes
    has the string from the DB row before enum conversion). Defaults
    to DeepSky for unknown kinds — better to produce a sane plan with
    aesthetic defaults than to crash mid-session."""
    if isinstance(kind, str):
        try:
            kind = WorkflowKind(kind)
        except ValueError:
            kind = WorkflowKind.DEEPSKY
    cls = WORKFLOWS.get(kind, DeepSkyWorkflow)
    return cls()


def list_workflows() -> list[WorkflowKind]:
    """Returns all registered workflow kinds in priority order."""
    return list(WORKFLOWS.keys())
