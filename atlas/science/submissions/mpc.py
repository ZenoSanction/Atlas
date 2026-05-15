"""MPC astrometric submission formatter.

The MPC accepts 80-column astrometric report format. Each observation is
one line with packed designation, observation timestamp, RA, Dec,
magnitude, observatory code.

TODO Phase 2: implement the full 80-column format builder.
"""
from __future__ import annotations

from atlas.db.managers import ConfigManager
from atlas.science.submissions.base import SubmissionPayload, Submitter


class MpcSubmitter(Submitter):
    destination = "mpc"

    def format(self, measurement_row: dict) -> SubmissionPayload:
        site = ConfigManager.get_site()
        obs_code = (site.observatory_code if site else None) or "XXX"
        # TODO Phase 2: pack designation, format epoch UTC to 5-decimal day,
        # RA hms.ss, Dec dms.s, magnitude, observatory code.
        text = (
            f"# TODO Phase 2: build MPC 80-col line\n"
            f"# observatory_code={obs_code}\n"
            f"# measurement: {measurement_row}\n"
        )
        return SubmissionPayload(text=text, metadata={"observatory_code": obs_code})

    async def send(self, payload: SubmissionPayload) -> dict:
        # TODO Phase 2: submit via email or web form per MPC's current API
        return {"ok": False, "error": "TODO Phase 2"}
