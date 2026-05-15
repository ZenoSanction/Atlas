"""ATLAS — Autonomous Telescope & Learning Astronomy System.

Five AI agents orchestrate a complete observatory, doing real scientific work
that gets submitted to MPC, AAVSO, TNS, and NASA Exoplanet Watch.

Public entry point: ``atlas.server.app`` (the FastAPI application).
CLI entry point: ``python -m atlas`` (see ``atlas.__main__``).
"""

__version__ = "1.0.0-phase1"
__all__ = ["__version__"]
