"""AAVSO variable-star / exoplanet photometry submission formatter.

AAVSO accepts AAVSO International Database (AID) format. Reference:
  https://www.aavso.org/aavso-extended-file-format

Header (comma-separated keys at top, # prefixed):
  #TYPE=EXTENDED
  #OBSCODE=ABCD
  #SOFTWARE=ATLAS 1.0
  #DELIM=,
  #DATE=JD
  #OBSTYPE=CCD
Then observation rows, one per line:
  NAME,DATE,MAGNITUDE,MAGERR,FILTER,TRANS,MTYPE,CNAME,CMAG,KNAME,KMAG,
       AIRMASS,GROUP,CHART,NOTES

  STAR     — variable star name (e.g. "BETA LYR", "Z UMA")
  DATE     — Julian Date with sufficient precision (e.g. 2459888.51234)
  MAGNITUDE — observed magnitude
  MAGERR   — 1-sigma error (na if not available)
  FILTER   — AAVSO filter code (V, B, R, I, TG = "green", etc.)
  TRANS    — YES if transformed to standard system, NO if instrumental
  MTYPE    — STD (standard / transformed) or DIF (differential) etc.
  CNAME    — comparison star name (or "ENSEMBLE")
  CMAG    — comparison star mag (or "na" for ensemble)
  KNAME    — check star name (or "na")
  KMAG    — check star observed mag (or "na")
  AIRMASS — at time of observation
  GROUP   — set/sequence ID (or "na")
  CHART   — AAVSO sequence chart ID (or "na")
  NOTES   — free text (or "na")
"""
from __future__ import annotations

from datetime import datetime

from atlas.db.managers import CredentialManager
from atlas.science.submissions.base import SubmissionPayload, Submitter


class AavsoFormatError(ValueError):
    pass


def _to_jd(dt: datetime) -> float:
    """UTC datetime → Julian Date. Meeus chapter 7."""
    import math
    y = dt.year
    m = dt.month
    d = (dt.day
         + (dt.hour + (dt.minute + (dt.second + dt.microsecond / 1e6) / 60.0) / 60.0) / 24.0)
    if m <= 2:
        y -= 1; m += 12
    a = math.floor(y / 100)
    b = 2 - a + math.floor(a / 4)
    return (math.floor(365.25 * (y + 4716))
              + math.floor(30.6001 * (m + 1))
              + d + b - 1524.5)


def _na(v) -> str:
    """AAVSO uses 'na' for missing values."""
    return "na" if v is None or v == "" else str(v)


def build_aavso_row(measurement_row: dict) -> str:
    """Build one AAVSO Extended-format observation row from a Measurement.

    measurement_row.value must include:
      star_name, magnitude, filter, comp_star (or "ENSEMBLE")
    Optionally:
      mag_err, transformed (bool), mtype (STD/DIF), comp_mag, check_star,
      check_mag, airmass, group, chart, notes
    """
    val = measurement_row.get("value") or {}
    epoch = measurement_row.get("epoch_utc")
    if epoch is None:
        raise AavsoFormatError("Measurement missing epoch_utc")
    if isinstance(epoch, str):
        epoch = datetime.fromisoformat(epoch.replace("Z", ""))
    star = val.get("star_name") or val.get("target_name")
    if not star:
        raise AavsoFormatError("Measurement.value missing star_name")
    if val.get("magnitude") is None:
        raise AavsoFormatError("Measurement.value missing magnitude")
    if not val.get("filter"):
        raise AavsoFormatError("Measurement.value missing filter")
    if not val.get("comp_star"):
        raise AavsoFormatError("Measurement.value missing comp_star (or 'ENSEMBLE')")

    jd = _to_jd(epoch)
    fields = [
        star.upper(),
        f"{jd:.5f}",
        f"{float(val['magnitude']):.3f}",
        f"{float(val['mag_err']):.3f}" if val.get("mag_err") is not None else "na",
        val["filter"].upper(),
        "YES" if val.get("transformed") else "NO",
        (val.get("mtype") or "STD").upper(),
        _na(val.get("comp_star")),
        f"{float(val['comp_mag']):.3f}" if val.get("comp_mag") is not None else "na",
        _na(val.get("check_star")),
        f"{float(val['check_mag']):.3f}" if val.get("check_mag") is not None else "na",
        f"{float(val['airmass']):.3f}" if val.get("airmass") is not None else "na",
        _na(val.get("group")),
        _na(val.get("chart")),
        _na(val.get("notes")),
    ]
    return ",".join(fields)


class AavsoSubmitter(Submitter):
    destination = "aavso"

    def format(self, measurement_row: dict) -> SubmissionPayload:
        observer_code = CredentialManager.get("aavso_observer_code") or ""
        if not observer_code:
            raise AavsoFormatError(
                "No AAVSO observer code configured. Set aavso_observer_code "
                "in Setup before queuing an AAVSO submission.")
        row = build_aavso_row(measurement_row)
        header = (
            "#TYPE=EXTENDED\n"
            f"#OBSCODE={observer_code}\n"
            "#SOFTWARE=ATLAS 1.0\n"
            "#DELIM=,\n"
            "#DATE=JD\n"
            "#OBSTYPE=CCD\n"
        )
        text = header + row + "\n"
        return SubmissionPayload(
            text=text,
            content_type="text/plain",
            metadata={"observer_code": observer_code,
                        "row": row},
        )

    async def send(self, payload: SubmissionPayload) -> dict:
        """AAVSO WebObs accepts file uploads at:
          https://www.aavso.org/apps/webobs/file/upload/
        as multipart/form-data with the file body as `obsfile`. This is
        what the WebObs web form posts. A real implementation would
        require operator login cookies + CSRF token; for now, return
        manual-upload guidance and let the operator copy the payload."""
        return {"ok": False,
                "error": ("send() not implemented for AAVSO in this pass. "
                            "Copy the formatted payload from the submission "
                            "queue and upload it manually to WebObs.")}
