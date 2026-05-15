"""Transient Name Server submission formatter.

TNS requires API token authentication and JSON payloads for both AT
(astronomical transient) reports and classification reports.

TODO Phase 2: implement JSON payload builder + HTTPS POST.
"""
from __future__ import annotations

from atlas.db.managers import CredentialManager
from atlas.science.submissions.base import SubmissionPayload, Submitter


class TnsSubmitter(Submitter):
    destination = "tns"
    TNS_API_BASE = "https://www.wis-tns.org/api/set"

    def format(self, measurement_row: dict) -> SubmissionPayload:
        bot_name = "ATLAS"
        # TODO Phase 2: build JSON per TNS API spec
        text = '{"TODO": "Phase 2 — TNS AT report JSON"}'
        return SubmissionPayload(
            text=text, content_type="application/json",
            metadata={"bot_name": bot_name},
        )

    async def send(self, payload: SubmissionPayload) -> dict:
        token = CredentialManager.get("tns_api_token")
        if not token:
            return {"ok": False, "error": "No TNS API token in credentials"}
        # TODO Phase 2: HTTPS POST to TNS_API_BASE with multipart form
        return {"ok": False, "error": "TODO Phase 2"}
