"""AAVSO variable-star / exoplanet photometry submission formatter.

AAVSO accepts AID (AAVSO International Database) format. TXT or AAVSO Extended.

TODO Phase 2: implement Extended format builder + WebObs upload.
"""
from __future__ import annotations

from atlas.db.managers import CredentialManager
from atlas.science.submissions.base import SubmissionPayload, Submitter


class AavsoSubmitter(Submitter):
    destination = "aavso"

    def format(self, measurement_row: dict) -> SubmissionPayload:
        observer_code = CredentialManager.get("aavso_observer_code") or "ATLAS"
        # TODO Phase 2: AAVSO Extended format header + observation rows
        text = (
            f"#TYPE=EXTENDED\n"
            f"#OBSCODE={observer_code}\n"
            f"#SOFTWARE=ATLAS\n"
            f"#DELIM=,\n"
            f"#DATE=JD\n"
            f"#OBSTYPE=CCD\n"
            f"# TODO Phase 2: observation rows\n"
        )
        return SubmissionPayload(text=text,
                                  metadata={"observer_code": observer_code})

    async def send(self, payload: SubmissionPayload) -> dict:
        # TODO Phase 2: AAVSO WebObs HTTP upload
        return {"ok": False, "error": "TODO Phase 2"}
