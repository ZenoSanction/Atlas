"""MPC astrometric submission formatter — real 80-column builder.

The IAU Minor Planet Center accepts observations in a fixed-width
80-column format. Each observation is one line:

  Cols  1-12 : Packed permanent / provisional / temporary designation
  Cols 13    : Discovery asterisk (* if a discovery observation)
  Cols 14    : Note 1 (single-letter, blank if none)
  Cols 15    : Note 2 — observation kind code:
                 C  CCD observation (what we always emit)
                 R  Radar; X  Roving observer; M  Meridian transit; ...
  Cols 16-32 : Observation date — YYYY MM DD.dddddd (5 decimal places of day)
  Cols 33-44 : RA — HH MM SS.ddd
  Cols 45-56 : Dec — sDD MM SS.dd (s = sign)
  Cols 57-65 : (blank or band/mag info)
  Cols 66-71 : Magnitude — MM.MM + band letter (e.g. "18.21V")
  Cols 72-77 : (blank)
  Cols 78-80 : IAU observatory code

Reference: https://minorplanetcenter.net/iau/info/OpticalObs.html

We don't do packed-designation generation for permanent numbered objects
— ATLAS amateurs working on NEOCP / new candidates use the
``temporary_designation`` (e.g. "NEO12345") which the MPC accepts as-is
in cols 6-12.

For full safety the formatter:
  * accepts an explicit Measurement.value dict (validated keys)
  * returns the 80-col line as text + a metadata block for the audit log
  * never silently truncates — raises on too-long fields
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from atlas.db.managers import ConfigManager
from atlas.science.submissions.base import SubmissionPayload, Submitter


class MpcFormatError(ValueError):
    """Raised when a measurement can't be formatted into MPC 80-col."""


def _fmt_designation(measurement_row: dict) -> str:
    """Pack the designation into cols 1-12. Pass either:
       packed_designation (12 chars, ready)
       OR temporary_designation (left-justified in cols 6-12, max 7 chars).
    """
    val = measurement_row.get("value") or {}
    packed = val.get("packed_designation")
    if packed:
        if len(packed) > 12:
            raise MpcFormatError(f"packed_designation > 12 chars: {packed!r}")
        return packed.ljust(12)
    temp = val.get("temporary_designation") or val.get("designation")
    if temp:
        if len(temp) > 7:
            raise MpcFormatError(
                f"temporary_designation > 7 chars: {temp!r}. Use packed_designation.")
        # Cols 1-5 blank, cols 6-12 left-justified
        return "     " + temp.ljust(7)
    raise MpcFormatError(
        "Measurement missing packed_designation or temporary_designation in value{}")


def _fmt_date(epoch_utc: datetime) -> str:
    """Cols 16-32. YYYY MM DD.dddddd. Total 17 chars including spaces."""
    # Fractional day in UTC
    seconds_into_day = (epoch_utc.hour * 3600
                         + epoch_utc.minute * 60
                         + epoch_utc.second
                         + epoch_utc.microsecond / 1_000_000.0)
    frac = seconds_into_day / 86400.0
    return f"{epoch_utc.year:04d} {epoch_utc.month:02d} {epoch_utc.day + frac:09.6f}"


def _fmt_ra(ra_deg: float) -> str:
    """Cols 33-44. HH MM SS.ddd (12 chars including spaces)."""
    hours = (ra_deg % 360.0) / 15.0
    h = int(hours)
    m_full = (hours - h) * 60.0
    m = int(m_full)
    s = (m_full - m) * 60.0
    return f"{h:02d} {m:02d} {s:06.3f}"


def _fmt_dec(dec_deg: float) -> str:
    """Cols 45-56. sDD MM SS.dd (12 chars including spaces)."""
    sign = "+" if dec_deg >= 0 else "-"
    abs_d = abs(dec_deg)
    d = int(abs_d)
    m_full = (abs_d - d) * 60.0
    m = int(m_full)
    s = (m_full - m) * 60.0
    return f"{sign}{d:02d} {m:02d} {s:05.2f}"


def _fmt_magnitude(mag: float | None, band: str | None) -> str:
    """Cols 66-71 (6 chars). Format: MM.MM[B]  (blank if no mag)."""
    if mag is None:
        return " " * 6
    band_char = (band or " ")[:1]
    return f"{mag:5.2f}{band_char}"


def build_mpc_line(measurement_row: dict, *, observatory_code: str) -> str:
    """Build the 80-column MPC report line for one astrometric measurement.

    measurement_row must have:
      epoch_utc (datetime),
      value = {ra_deg: float, dec_deg: float,
                packed_designation OR temporary_designation,
                optional magnitude, optional band ("V", "R", "G", ...)}.

    Returns exactly 80 characters."""
    val = measurement_row.get("value") or {}
    epoch = measurement_row.get("epoch_utc")
    if epoch is None:
        raise MpcFormatError("Measurement missing epoch_utc")
    if isinstance(epoch, str):
        epoch = datetime.fromisoformat(epoch.replace("Z", ""))
    if "ra_deg" not in val or "dec_deg" not in val:
        raise MpcFormatError("Measurement.value missing ra_deg / dec_deg")

    if not (len(observatory_code) == 3):
        raise MpcFormatError(
            f"observatory_code must be exactly 3 chars, got {observatory_code!r}")

    designation = _fmt_designation(measurement_row)   # 12 cols
    discovery = " "                                    # col 13 (not a discovery)
    note1 = " "                                        # col 14
    note2 = "C"                                        # col 15 — CCD observation
    date = _fmt_date(epoch)                            # 17 cols (16-32)
    ra = _fmt_ra(float(val["ra_deg"]))                 # 12 cols (33-44)
    dec = _fmt_dec(float(val["dec_deg"]))              # 12 cols (45-56)
    band = val.get("band")
    mag = val.get("magnitude")
    blank_after_dec = " " * 9                          # cols 57-65 (band info area; blank)
    mag_field = _fmt_magnitude(float(mag) if mag is not None else None, band)  # 6 cols
    spacer = " " * 6                                   # cols 72-77
    site = observatory_code                            # cols 78-80

    line = (designation + discovery + note1 + note2
            + date + ra + dec + blank_after_dec + mag_field + spacer + site)
    if len(line) != 80:
        raise MpcFormatError(
            f"Internal: assembled line is {len(line)} chars, expected 80. "
            f"Line: {line!r}")
    return line


class MpcSubmitter(Submitter):
    destination = "mpc"

    def format(self, measurement_row: dict) -> SubmissionPayload:
        site = ConfigManager.get_site()
        obs_code = ((site.observatory_code if site else None) or "").strip()
        if not obs_code:
            raise MpcFormatError(
                "No MPC observatory_code configured. Set site.observatory_code "
                "in Setup before queuing an MPC submission.")
        line = build_mpc_line(measurement_row, observatory_code=obs_code)
        # MPC reports may contain a header block + multiple observation
        # lines; for now we emit a single-line payload + a header that the
        # operator can edit before submission.
        header = (
            "COD " + obs_code + "\n"
            f"OBS {(site.observatory_name or 'ATLAS Observatory') if site else 'ATLAS Observatory'}\n"
            "MEA \n"   # measurer (operator name) — fill before submitting
            "TEL \n"   # telescope description — fill before submitting
            "NET Gaia DR3\n"
            "ACK ATLAS auto-generated\n"
            "AC2 \n"   # acknowledgement email — fill before submitting
        )
        text = header + line + "\n"
        return SubmissionPayload(
            text=text,
            content_type="text/plain",
            metadata={"observatory_code": obs_code,
                        "line_length": len(line)},
        )

    async def send(self, payload: SubmissionPayload) -> dict:
        # MPC accepts via email gateway (obs@cfa.harvard.edu).
        # Implementing the SMTP send is in pass 2D's submission_send
        # framework — for now, the formatted text is what the operator
        # would copy into their MPC submission email.
        return {"ok": False,
                "error": ("send() not implemented in this pass. "
                            "Copy the formatted payload from the submission "
                            "queue and email it manually for now.")}
