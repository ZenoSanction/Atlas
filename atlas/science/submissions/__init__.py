"""Submission formatters.

Every formatter implements ``Submitter`` so the Submission table machinery
can prepare and send a measurement to its external destination.

No formatter ever sends without an operator-approved Submission row.
"""
from atlas.science.submissions.base import Submitter, SubmissionPayload

__all__ = ["Submitter", "SubmissionPayload"]
