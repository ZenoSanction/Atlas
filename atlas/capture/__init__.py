"""Capture subsystem — file ingestion, FITS reading, calibration library.

Phase 2 prerequisite: turn captured FITS files into rows in the ``frames``
and ``calibration_masters`` tables so the rest of the science workflows
(astrometry, photometry, etc.) have something to read.
"""
from atlas.capture.ingest import (
    read_fits_header, register_frame, register_calibration_master,
    ingest_directory,
)

__all__ = [
    "read_fits_header",
    "register_frame",
    "register_calibration_master",
    "ingest_directory",
]
