"""Transient Name Server (TNS) submission formatter.

TNS is the official ICAU clearinghouse for new transients (supernovae,
novae, tidal disruption events, etc.). Reports go to a JSON API:

  POST https://www.wis-tns.org/api/set/bulk-report

The bulk_report endpoint accepts a multipart form with a 'data' field
containing JSON like:

  {
    "at_report": {
        "0": {
            "ra":         {"value": "12:34:56.78", "error": "0.5", "units": "arcsec"},
            "dec":        {"value": "+01:23:45.6", "error": "0.5", "units": "arcsec"},
            "reporting_group_id":  74,             # ATLAS group id
            "discovery_datetime":  "2026-05-22 04:30:00",
            "at_type":             1,              # 1=PSN (supernova candidate)
            "host_name":           "NGC 1234",     # optional
            "host_redshift":       0.012,           # optional
            "transient_redshift":  null,
            "internal_name":       "ATLAS25abc",
            "remarks":             "",
            "discovery_data_source": {"groupid": 74},
            "non_detection": {
                "obsdate":             "2026-05-15 04:30:00",
                "limiting_flux":       21.0,
                "flux_units":           "Magnitude",
                "filter_value":         22,    # 22=L (clear)
                "instrument_value":     104    # ATLAS
            },
            "photometry": {
                "photometry_group": {
                    "0": {
                        "obsdate":         "2026-05-22 04:30:00",
                        "flux":            18.5,
                        "flux_error":      0.05,
                        "limiting_flux":   "",
                        "flux_units":      "Magnitude",
                        "filter_value":    22,
                        "instrument_value": 104,
                        "exptime":         120,
                        "observer":        "ATLAS Bot",
                        "comments":        "Detected against PS1 reference"
                    }
                }
            }
        }
    }
  }

This module owns the format step only. The send step posts the JSON
to TNS_API_BASE with the API token in the headers — wired on bench
day when we have a real candidate to submit.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from atlas.db.managers import CredentialManager
from atlas.science.submissions.base import SubmissionPayload, Submitter


# TNS filter codes (subset). Full list: https://www.wis-tns.org/api/values
TNS_FILTER_CODES = {
    "L":   22,  # Clear / luminance
    "R":   13,  # Cousins R
    "G":   12,  # Cousins V
    "B":   10,  # Bessell B
    "V":   11,  # Bessell V
    "Rc":  13,
    "Ic":  14,
    "Ha":  21,  # H-alpha
    "OIII": 35,
    "SII": 36,
    "u":    1, "g":    3, "r":    4, "i":    5, "z":    6,
}

# TNS at_type codes
AT_TYPE_PSN = 1            # possible supernova
AT_TYPE_VARIABLE = 4
AT_TYPE_NUCLEAR = 5
AT_TYPE_AGN = 6


def _deg_to_sexagesimal_ra(ra_deg: float) -> str:
    """Convert RA in degrees to HH:MM:SS.ss."""
    hours = (ra_deg % 360.0) / 15.0
    h = int(hours)
    m_full = (hours - h) * 60.0
    m = int(m_full)
    s = (m_full - m) * 60.0
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def _deg_to_sexagesimal_dec(dec_deg: float) -> str:
    """Convert Dec in degrees to +DD:MM:SS.s."""
    sign = "+" if dec_deg >= 0 else "-"
    d = abs(dec_deg)
    deg = int(d)
    m_full = (d - deg) * 60.0
    m = int(m_full)
    s = (m_full - m) * 60.0
    return f"{sign}{deg:02d}:{m:02d}:{s:04.1f}"


class TnsSubmitter(Submitter):
    destination = "tns"
    TNS_API_BASE = "https://www.wis-tns.org/api/set"
    # ATLAS reporting group id — placeholder until we register with TNS.
    # The real id arrives in our TNS account confirmation email.
    REPORTING_GROUP_ID = 74      # bench day: set to real value
    INSTRUMENT_ID = 104          # CMOS — refine per real camera registration

    def __init__(self, reporting_group_id: Optional[int] = None,
                   instrument_id: Optional[int] = None,
                   bot_name: str = "ATLAS"):
        self._group_id = reporting_group_id or self.REPORTING_GROUP_ID
        self._instrument_id = instrument_id or self.INSTRUMENT_ID
        self._bot_name = bot_name

    def format(self, measurement_row: dict) -> SubmissionPayload:
        """Build a TNS AT report JSON from one transient measurement.

        Expected measurement_row.value shape (from TransientWorkflow
        process() output):
            {
              "ra_deg": float, "dec_deg": float,
              "ra_err_arcsec": float, "dec_err_arcsec": float,
              "mag": float, "mag_err": float, "filter": "L"|...,
              "snr": float, "exposure_s": float,
              "discovery_datetime_utc": ISO string,
              "internal_name": str,                  # optional
              "host_name": str,                       # optional
              "host_redshift": float,                 # optional
              "non_detection": {                      # optional
                  "obsdate_utc": ISO, "limiting_mag": float,
                  "filter": "L"|...
              },
              "remarks": str,                          # optional
            }
        """
        v = measurement_row.get("value") or {}
        ra_deg = float(v.get("ra_deg", 0.0))
        dec_deg = float(v.get("dec_deg", 0.0))
        filt = (v.get("filter") or "L").upper()
        flux = float(v.get("mag", 0.0))
        flux_err = float(v.get("mag_err", 0.1))
        exposure = float(v.get("exposure_s", 60.0))
        discovery_dt = v.get("discovery_datetime_utc") or (
            datetime.utcnow().isoformat(timespec="seconds") + " UTC"
        )
        # Normalize to TNS-friendly format YYYY-MM-DD HH:MM:SS
        if "T" in discovery_dt:
            discovery_dt = discovery_dt.replace("T", " ").rstrip("Z")

        report = {
            "ra": {"value": _deg_to_sexagesimal_ra(ra_deg),
                     "error": str(v.get("ra_err_arcsec", 0.5)),
                     "units": "arcsec"},
            "dec": {"value": _deg_to_sexagesimal_dec(dec_deg),
                      "error": str(v.get("dec_err_arcsec", 0.5)),
                      "units": "arcsec"},
            "reporting_group_id": self._group_id,
            "discovery_datetime": discovery_dt,
            "at_type": AT_TYPE_PSN,
            "host_name": v.get("host_name") or "",
            "host_redshift": v.get("host_redshift"),
            "transient_redshift": None,
            "internal_name": v.get("internal_name") or "",
            "remarks": v.get("remarks") or "",
            "discovery_data_source": {"groupid": self._group_id},
            "photometry": {
                "photometry_group": {
                    "0": {
                        "obsdate": discovery_dt,
                        "flux": round(flux, 3),
                        "flux_error": round(flux_err, 3),
                        "limiting_flux": "",
                        "flux_units": "Magnitude",
                        "filter_value": TNS_FILTER_CODES.get(filt, 22),
                        "instrument_value": self._instrument_id,
                        "exptime": exposure,
                        "observer": self._bot_name,
                        "comments": v.get("photometry_comment")
                                       or "ATLAS automated detection",
                    },
                },
            },
        }

        # Optional non-detection block (strongly recommended by TNS)
        nd = v.get("non_detection")
        if nd:
            nd_date = nd.get("obsdate_utc") or ""
            if "T" in nd_date:
                nd_date = nd_date.replace("T", " ").rstrip("Z")
            report["non_detection"] = {
                "obsdate": nd_date,
                "limiting_flux": float(nd.get("limiting_mag", 21.0)),
                "flux_units": "Magnitude",
                "filter_value": TNS_FILTER_CODES.get(
                    (nd.get("filter") or "L").upper(), 22),
                "instrument_value": self._instrument_id,
            }

        payload_dict = {
            "at_report": {"0": report},
        }
        text = json.dumps(payload_dict, indent=2)
        return SubmissionPayload(
            text=text, content_type="application/json",
            metadata={"bot_name": self._bot_name,
                        "reporting_group_id": self._group_id,
                        "at_type": "PSN",
                        "ra_deg": ra_deg, "dec_deg": dec_deg,
                        "filter": filt, "mag": flux},
        )

    async def send(self, payload: SubmissionPayload) -> dict:
        """POST the AT report to TNS. Wired bench day with httpx —
        TNS uses multipart form with the 'data' field carrying the
        JSON and the 'api_key' header for auth."""
        token = CredentialManager.get("tns_api_token")
        if not token:
            return {"ok": False,
                      "error": "No TNS API token in credentials vault"}
        # bench day: replace this with httpx.AsyncClient call. The shape:
        #   headers = {"User-Agent": json.dumps({"tns_id": GROUP_ID,
        #                                          "type": "bot",
        #                                          "name": BOT_NAME})}
        #   data = {"api_key": token, "data": payload.text}
        #   POST to {self.TNS_API_BASE}/bulk-report
        return {"ok": False,
                  "error": ("TNS endpoint not yet wired — payload formatted "
                              "and ready, bench day completes the POST"),
                  "preview_first_120_chars": payload.text[:120]}
