"""NASA Exoplanet Watch submission formatter.

Exoplanet Watch accepts AAVSO Exoplanet Section-compatible format plus
exofop submission. Light curves are usually CSV with JD_UTC, normalized flux,
flux uncertainty, plus a header block.

TODO Phase 2.
"""
from __future__ import annotations

from atlas.science.submissions.base import SubmissionPayload, Submitter


class NasaEoSubmitter(Submitter):
    destination = "nasa_exoplanet_watch"

    def format(self, measurement_row: dict) -> SubmissionPayload:
        text = "# TODO Phase 2: NASA Exoplanet Watch light-curve CSV\n"
        return SubmissionPayload(text=text, content_type="text/csv")

    async def send(self, payload: SubmissionPayload) -> dict:
        # TODO Phase 2: Exoplanet Watch upload endpoint
        return {"ok": False, "error": "TODO Phase 2"}
