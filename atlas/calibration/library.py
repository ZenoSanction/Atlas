"""Calibration library manager — coverage, staleness, best-match.

The DB stores raw CalibrationMaster rows but knows nothing about
*which* masters we should have for tonight's equipment + filters +
exposure plan. That's what this module figures out.

Coverage report answers: "what calibration work is missing?" Three
classes of master, three different rules of thumb:

  * Bias — sensor read pattern, basically constant with gain/offset.
           One per (gain, offset, temperature) combo. Stale after
           90 days (sensors do drift slowly).
  * Dark — thermal + hot-pixel signature. One per
           (exposure_s, gain, offset, temperature) combo. Stale after
           30 days (hot pixels develop, cooling can drift).
  * Flat — optical train + dust spots. One per (filter, gain, offset)
           combo, ignoring temperature. Stale after 7 days because dust
           moves any time you touch the rig.

"Best match" lookup tolerates small mismatches: a dark within ±1 °C
and ±5 s of exposure is usable (within reason). This is what NINA /
PixInsight / Siril all do — exact-match would be impossibly strict
for a CMOS sensor whose set-point drifts ±0.5 °C.

The library doesn't capture frames itself — it just reads from
the DB and reports. Capture orchestration (twilight flats, dark
runs) lives in `atlas.calibration.capture_orchestrator` (to come).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from atlas.db.models import CalibrationMaster, Frame
from atlas.db.session import get_session
from atlas.logging_setup import get_logger

log = get_logger("calibration.library")


# ---- staleness rules (days) ----------------------------------------------

BIAS_FRESHNESS_DAYS = 90    # sensor pattern stable for months
DARK_FRESHNESS_DAYS = 30    # hot pixels develop; thermal can drift
FLAT_FRESHNESS_DAYS = 7     # dust moves any time you touch the rig


# ---- tolerances for "best match" lookup ----------------------------------

DARK_TEMP_TOL_C = 1.0       # dark temp within ±1 °C of light temp
DARK_EXP_TOL_FRAC = 0.20    # dark exposure within ±20% of light exposure
BIAS_TEMP_TOL_C = 3.0       # bias is less temp-sensitive than dark


# ---- dataclasses ---------------------------------------------------------

@dataclass
class MasterMatch:
    """Result of a best-match lookup for one (kind + light frame
    parameters) combo. ``master`` is None when nothing usable was
    found; ``reason`` always explains why."""
    kind: str                      # 'bias' | 'dark' | 'flat'
    master: Optional[CalibrationMaster]
    fresh: bool = False
    reason: str = ""
    requested: dict = field(default_factory=dict)


@dataclass
class CalibrationCoverage:
    """A full snapshot of the calibration library against the
    equipment's recent capture patterns."""
    bias: list[dict] = field(default_factory=list)
    dark: list[dict] = field(default_factory=list)
    flat: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)   # combos w/ no master at all
    stale: list[dict] = field(default_factory=list)     # have master but past freshness
    summary: str = ""

    def to_jsonable(self) -> dict:
        return {
            "bias": self.bias, "dark": self.dark, "flat": self.flat,
            "missing": self.missing, "stale": self.stale,
            "summary": self.summary,
        }


# ---- core class ----------------------------------------------------------

class CalibrationLibrary:
    """Read-side queries over CalibrationMaster + Frame.

    Stateless — every method opens its own DB session and returns
    detached / dict results. Safe to instantiate freely (e.g. from
    preflight loop)."""

    # ------------- staleness lookup -------------

    def freshness_days(self, kind: str) -> int:
        return {
            "bias": BIAS_FRESHNESS_DAYS,
            "dark": DARK_FRESHNESS_DAYS,
            "flat": FLAT_FRESHNESS_DAYS,
        }.get(kind, 30)

    def is_fresh(self, master: CalibrationMaster) -> bool:
        if master is None:
            return False
        age = datetime.utcnow() - master.created_at
        return age.days < self.freshness_days(master.kind)

    # ------------- best-match -------------

    def best_match(self, *, kind: str,
                     exposure_s: Optional[float] = None,
                     filter_name: Optional[str] = None,
                     ccd_temp_c: Optional[float] = None,
                     gain: Optional[int] = None,
                     offset: Optional[int] = None,
                     ) -> MasterMatch:
        """Find the most-applicable master for a given light-frame
        configuration. Tolerances applied per `kind`."""
        requested = {
            "exposure_s": exposure_s, "filter_name": filter_name,
            "ccd_temp_c": ccd_temp_c, "gain": gain, "offset": offset,
        }
        with get_session() as s:
            q = s.query(CalibrationMaster).filter_by(kind=kind)
            if gain is not None:
                q = q.filter(CalibrationMaster.gain == gain)
            if offset is not None:
                q = q.filter(CalibrationMaster.offset == offset)
            if kind == "flat" and filter_name is not None:
                q = q.filter(CalibrationMaster.filter_name == filter_name)
            candidates = q.order_by(CalibrationMaster.created_at.desc()).all()
            for c in candidates:
                s.expunge(c)

        if not candidates:
            return MasterMatch(
                kind=kind, master=None, fresh=False,
                reason="no masters found for the requested params",
                requested=requested,
            )

        # Score each candidate; lowest score wins. Components:
        #   * disqualifying mismatch (exposure too far, temp too far) -> skip
        #   * temp delta (smaller is better)
        #   * exposure delta (smaller is better)
        #   * age (older bumps score)
        best = None
        best_score = float("inf")
        for c in candidates:
            score = 0.0
            # Hard gates per kind
            if kind == "dark":
                if exposure_s is not None and c.exposure_s is not None:
                    frac = abs(c.exposure_s - exposure_s) / max(exposure_s, 0.001)
                    if frac > DARK_EXP_TOL_FRAC:
                        continue
                    score += frac * 100
                if ccd_temp_c is not None and c.ccd_temp_c is not None:
                    dt = abs(c.ccd_temp_c - ccd_temp_c)
                    if dt > DARK_TEMP_TOL_C:
                        continue
                    score += dt * 10
            elif kind == "bias":
                if ccd_temp_c is not None and c.ccd_temp_c is not None:
                    dt = abs(c.ccd_temp_c - ccd_temp_c)
                    if dt > BIAS_TEMP_TOL_C:
                        continue
                    score += dt * 5
            # Age penalty (older = bigger)
            age_days = (datetime.utcnow() - c.created_at).days
            score += age_days * 0.5
            if score < best_score:
                best_score = score
                best = c

        if best is None:
            return MasterMatch(
                kind=kind, master=None, fresh=False,
                reason=("masters exist but none within tolerances "
                          f"(exp ±{DARK_EXP_TOL_FRAC:.0%}, "
                          f"temp ±{DARK_TEMP_TOL_C:.1f}°C for darks)"),
                requested=requested,
            )

        fresh = self.is_fresh(best)
        return MasterMatch(
            kind=kind, master=best, fresh=fresh,
            reason=("fresh match" if fresh
                      else f"matched but {(datetime.utcnow() - best.created_at).days} "
                            f"days old (freshness {self.freshness_days(kind)}d)"),
            requested=requested,
        )

    # ------------- coverage report -------------

    def coverage_report(self, *, recent_days: int = 30) -> CalibrationCoverage:
        """Build a coverage report by cross-referencing CalibrationMaster
        rows against recent light frames in the DB.

        For each (filter, exposure_s, gain, offset, temp) combo seen in
        recent lights, ask: do we have a fresh dark / bias / flat for it?
        Anything missing or stale lands in the report.

        ``recent_days`` defines "recent" — only light frames captured in
        that window are considered. Default 30 days catches a typical
        multi-session campaign without dragging in stale combos."""
        coverage = CalibrationCoverage()
        cutoff = datetime.utcnow() - timedelta(days=recent_days)

        with get_session() as s:
            # Distinct light-frame configurations seen recently
            rows = (s.query(
                        Frame.filter_name, Frame.exposure_s,
                        Frame.gain, Frame.offset, Frame.ccd_temp_c)
                      .filter(Frame.frame_type == "light",
                                Frame.captured_at >= cutoff)
                      .distinct().all())
            # Group: round temp to 1°C, exposure to nearest second
            seen_combos = []
            for filter_name, exp, gain, offset, temp in rows:
                if exp is None:
                    continue
                seen_combos.append({
                    "filter": filter_name,
                    "exposure_s": round(float(exp), 1),
                    "gain": gain, "offset": offset,
                    "ccd_temp_c": (round(float(temp), 1) if temp is not None
                                     else None),
                })

            # All masters, broken out by kind
            masters = s.query(CalibrationMaster).all()
            for m in masters:
                s.expunge(m)

        # Group masters
        by_kind = {"bias": [], "dark": [], "flat": []}
        now = datetime.utcnow()
        for m in masters:
            if m.kind not in by_kind:
                continue
            fresh = (now - m.created_at).days < self.freshness_days(m.kind)
            entry = {
                "id": m.id, "kind": m.kind,
                "filter_name": m.filter_name, "exposure_s": m.exposure_s,
                "ccd_temp_c": m.ccd_temp_c, "gain": m.gain, "offset": m.offset,
                "n_frames": m.n_frames,
                "age_days": (now - m.created_at).days,
                "fresh": fresh,
            }
            by_kind[m.kind].append(entry)
            if not fresh:
                coverage.stale.append(entry)
        coverage.bias = by_kind["bias"]
        coverage.dark = by_kind["dark"]
        coverage.flat = by_kind["flat"]

        # For each seen combo, check what's missing
        seen_filters = sorted({c["filter"] for c in seen_combos if c["filter"]})
        seen_exposures = sorted({c["exposure_s"] for c in seen_combos})
        seen_gain_offset = sorted({(c["gain"], c["offset"])
                                       for c in seen_combos})

        # Bias: one per (gain, offset). Temperature gets a wider tolerance.
        for (gain, offset) in seen_gain_offset:
            match = self.best_match(kind="bias", gain=gain, offset=offset)
            if match.master is None:
                coverage.missing.append({
                    "kind": "bias", "gain": gain, "offset": offset,
                    "why": "no bias master for this gain/offset combo",
                })

        # Dark: one per (exposure, gain, offset). Temp checked when present.
        for combo in seen_combos:
            match = self.best_match(
                kind="dark",
                exposure_s=combo["exposure_s"],
                gain=combo["gain"], offset=combo["offset"],
                ccd_temp_c=combo["ccd_temp_c"],
            )
            if match.master is None:
                coverage.missing.append({
                    "kind": "dark",
                    "exposure_s": combo["exposure_s"],
                    "gain": combo["gain"], "offset": combo["offset"],
                    "ccd_temp_c": combo["ccd_temp_c"],
                    "why": match.reason,
                })

        # Flat: one per (filter, gain, offset). Most frequently rebuilt.
        for filter_name in seen_filters:
            for (gain, offset) in seen_gain_offset:
                match = self.best_match(
                    kind="flat", filter_name=filter_name,
                    gain=gain, offset=offset,
                )
                if match.master is None:
                    coverage.missing.append({
                        "kind": "flat", "filter": filter_name,
                        "gain": gain, "offset": offset,
                        "why": "no flat master for this filter/gain combo",
                    })

        # Dedupe missing entries (multiple lights with same params would
        # otherwise produce duplicates).
        seen_keys = set()
        deduped: list[dict] = []
        for m in coverage.missing:
            key = (m.get("kind"), m.get("filter"),
                     m.get("exposure_s"), m.get("gain"),
                     m.get("offset"), m.get("ccd_temp_c"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(m)
        coverage.missing = deduped

        # Summary line
        n_total = len(coverage.bias) + len(coverage.dark) + len(coverage.flat)
        n_stale = len(coverage.stale)
        n_missing = len(coverage.missing)
        if n_total == 0:
            coverage.summary = ("calibration library is EMPTY — "
                                  "no masters of any kind found")
        elif n_missing == 0 and n_stale == 0:
            coverage.summary = (f"calibration library: {n_total} masters, "
                                  "all fresh, full coverage for recent lights")
        else:
            parts = [f"{n_total} masters"]
            if n_missing > 0:
                parts.append(f"{n_missing} missing combo(s)")
            if n_stale > 0:
                parts.append(f"{n_stale} stale")
            coverage.summary = "calibration library: " + ", ".join(parts)
        return coverage

    # ------------- recommended action -------------

    def recommended_actions(self) -> list[dict]:
        """Turn a coverage report into a prioritized action list the
        Operator + dashboard can show. Each action is one calibration
        capture run we should do soon. Most-urgent first."""
        cov = self.coverage_report()
        actions: list[dict] = []
        # Flats first — dust changes every time you breathe on the OTA
        for m in cov.missing:
            if m.get("kind") == "flat":
                actions.append({
                    "priority": "high",
                    "kind": "flat",
                    "filter": m.get("filter"),
                    "gain": m.get("gain"), "offset": m.get("offset"),
                    "summary": (f"capture flat for filter "
                                  f"{m.get('filter')} (gain {m.get('gain')})"),
                })
        # Then darks (per-exposure)
        for m in cov.missing:
            if m.get("kind") == "dark":
                actions.append({
                    "priority": "high",
                    "kind": "dark",
                    "exposure_s": m.get("exposure_s"),
                    "gain": m.get("gain"), "offset": m.get("offset"),
                    "ccd_temp_c": m.get("ccd_temp_c"),
                    "summary": (f"capture {m.get('exposure_s')}s dark @ "
                                  f"gain {m.get('gain')}"),
                })
        # Bias least urgent
        for m in cov.missing:
            if m.get("kind") == "bias":
                actions.append({
                    "priority": "medium",
                    "kind": "bias",
                    "gain": m.get("gain"), "offset": m.get("offset"),
                    "summary": f"capture bias @ gain {m.get('gain')}",
                })
        # Stale masters: queue refresh, lower priority
        for entry in cov.stale:
            actions.append({
                "priority": "low",
                "kind": entry["kind"],
                "filter": entry.get("filter_name"),
                "exposure_s": entry.get("exposure_s"),
                "gain": entry.get("gain"), "offset": entry.get("offset"),
                "ccd_temp_c": entry.get("ccd_temp_c"),
                "summary": (f"refresh {entry['kind']} master "
                              f"(currently {entry['age_days']}d old)"),
            })
        return actions
