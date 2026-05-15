"""Submission base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SubmissionPayload:
    """A submission-ready payload."""
    text: str        # The exact content that will be sent to the external service
    content_type: str = "text/plain"
    metadata: dict | None = None


class Submitter(ABC):
    """ABC for all external-service submitters."""

    destination: str  # subclass-set, matches SubmissionDestination value

    @abstractmethod
    def format(self, measurement_row: dict) -> SubmissionPayload:
        """Convert a measurement (and any required context) into a payload."""

    @abstractmethod
    async def send(self, payload: SubmissionPayload) -> dict:
        """Send the payload to the destination. Returns the response."""
