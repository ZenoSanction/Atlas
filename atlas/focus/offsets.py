"""Per-filter focuser-step offsets.

Mono setups need a different focus position per filter (the filter
glass shifts the focal plane). Doing a full V-curve autofocus on
every filter change costs 60-90 s of sky time — across an LRGB
session that's 4-6 minutes of pure overhead. After the first night,
the offsets between filters are *stable* on most rigs (the optical
train doesn't move much). So:

  1. Run AF on the reference filter (typically L) — note the focuser
     position.
  2. Run AF on each other filter once, record (filter_step - L_step)
     as that filter's offset.
  3. On subsequent filter changes during the session (or future
     sessions if the offset table is fresh), apply the offset
     directly: target_step = L_step + filter_offset. Skip AF.

This module owns:
  * record_af(filter, focuser_step)  -> updates table, computes offset
  * offset_for(filter)               -> returns offset vs reference
  * decide_filter_change(...)         -> "should we skip AF and just
                                          jump, or do we still need
                                          to AF here?"

Decision logic:
  * Reference filter never gets an offset; always AFs at session
    start.
  * Non-reference filter with a known offset + recent AF on reference
    -> skip AF, apply offset (emit a `focus_offset_applied` event).
  * Non-reference filter without an offset, or stale data -> full AF
    (gives us the data to populate / refresh the table).
  * Hardware report says step doesn't match (verify after jump) ->
    fall back to AF.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from atlas.logging_setup import get_logger

log = get_logger("focus.offsets")


# How long an offset measurement stays trustworthy. After this, the
# Operator should re-measure (the rig may have drifted from temp /
# mechanical settling). 30 days is conservative for amateur rigs.
OFFSET_FRESHNESS_DAYS = 30


@dataclass
class FilterOffsetEntry:
    """One recorded offset: focuser steps relative to the reference
    filter at the moment of measurement."""
    filter_name: str
    offset_steps: int
    reference_filter: str
    recorded_at: datetime
    temperature_c: Optional[float] = None
    af_hfr: Optional[float] = None    # HFR achieved by the AF run

    def is_fresh(self) -> bool:
        return (datetime.utcnow() - self.recorded_at).days < OFFSET_FRESHNESS_DAYS

    def to_jsonable(self) -> dict:
        return {
            "filter_name": self.filter_name,
            "offset_steps": int(self.offset_steps),
            "reference_filter": self.reference_filter,
            "recorded_at": self.recorded_at.isoformat(timespec="seconds") + "Z",
            "temperature_c": self.temperature_c,
            "af_hfr": self.af_hfr,
            "fresh": self.is_fresh(),
            "age_days": (datetime.utcnow() - self.recorded_at).days,
        }


@dataclass
class FocusJumpDecision:
    """Outcome of decide_filter_change(): tells the Operator whether
    to apply a known offset jump or run a full AF run for this filter."""
    action: str               # "jump" | "autofocus" | "no_change"
    target_step: Optional[int] = None
    offset_applied: Optional[int] = None
    reason: str = ""


class FilterOffsetTable:
    """Read/write access to the per-filter offset table stored on
    EquipmentProfile.filter_offsets (JSON dict).

    Stateless wrapper — every call hits the DB. Cheap; offsets are
    measured once per night at most."""

    def __init__(self) -> None:
        pass

    # ---- read ----

    def reference_filter(self) -> str:
        from atlas.db.managers import ConfigManager
        eq = ConfigManager.get_equipment()
        return (eq.filter_offset_reference if eq else "L") or "L"

    def get(self, filter_name: str) -> Optional[FilterOffsetEntry]:
        """Return the recorded offset entry for ``filter_name`` (or
        None if not yet measured)."""
        from atlas.db.managers import ConfigManager
        eq = ConfigManager.get_equipment()
        if eq is None:
            return None
        table = (eq.filter_offsets or {}) or {}
        raw = table.get(filter_name.upper())
        if not raw:
            return None
        try:
            recorded_at = datetime.fromisoformat(
                raw["recorded_at"].rstrip("Z"))
        except Exception:
            recorded_at = datetime.utcnow()
        return FilterOffsetEntry(
            filter_name=filter_name.upper(),
            offset_steps=int(raw.get("offset_steps", 0)),
            reference_filter=raw.get("reference_filter") or self.reference_filter(),
            recorded_at=recorded_at,
            temperature_c=raw.get("temperature_c"),
            af_hfr=raw.get("af_hfr"),
        )

    def all_entries(self) -> dict[str, FilterOffsetEntry]:
        from atlas.db.managers import ConfigManager
        eq = ConfigManager.get_equipment()
        if eq is None:
            return {}
        out: dict[str, FilterOffsetEntry] = {}
        for filt in (eq.filter_offsets or {}).keys():
            entry = self.get(filt)
            if entry is not None:
                out[filt.upper()] = entry
        return out

    # ---- write ----

    def record_af(self, *, filter_name: str, focuser_step: int,
                     reference_step: Optional[int] = None,
                     temperature_c: Optional[float] = None,
                     af_hfr: Optional[float] = None) -> None:
        """Record an autofocus result. If ``reference_step`` is None,
        we look up the most recent reference-filter result and use it
        — but the caller should pass it explicitly when known to
        avoid drift.

        For the reference filter itself, this writes the reference
        step (offset = 0) so other filters can use it as the baseline.
        """
        from atlas.db.managers import ConfigManager
        from atlas.db.models import EquipmentProfile
        from atlas.db.session import get_session

        ref_filt = self.reference_filter().upper()
        filt = filter_name.upper()
        offset = 0
        if filt != ref_filt:
            # Need the most recent reference step to compute offset
            if reference_step is None:
                ref_entry = self.get(ref_filt)
                if ref_entry is None:
                    log.warning("record_af for %s but no reference "
                                  "filter %s position on file — "
                                  "recording offset=0", filt, ref_filt)
                else:
                    # Reference entry stores offset_steps as its absolute
                    # step (since we treat ref as offset=0 from itself,
                    # the recorded number is the absolute step it AF'd to).
                    reference_step = ref_entry.offset_steps
            if reference_step is not None:
                offset = int(focuser_step) - int(reference_step)
        else:
            # Reference filter — store its absolute step so offsets
            # for other filters can be computed against it.
            offset = int(focuser_step)

        entry = {
            "offset_steps": offset,
            "reference_filter": ref_filt,
            "recorded_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "temperature_c": temperature_c,
            "af_hfr": af_hfr,
            "absolute_step": int(focuser_step),
        }
        with get_session() as s:
            eq = s.query(EquipmentProfile).first()
            if eq is None:
                return
            tbl = dict(eq.filter_offsets or {})
            tbl[filt] = entry
            eq.filter_offsets = tbl
        log.info("filter offset recorded: %s offset=%d (ref %s, "
                  "abs step %d, hfr %s)",
                  filt, offset, ref_filt, focuser_step, af_hfr)


# ---- decision engine -----------------------------------------------------

def decide_filter_change(*, from_filter: Optional[str], to_filter: str,
                            current_focuser_step: Optional[int] = None,
                            ) -> FocusJumpDecision:
    """Should the Operator skip AF and jump to a known offset, or run
    a full AF run on the new filter?

    Returns one of:
      * action="jump"        — apply offset, no AF
      * action="autofocus"   — must AF to populate/refresh table
      * action="no_change"   — same filter, nothing to do

    The orchestrator emits the decision event so the human sees why
    AF was or wasn't run on this filter change. Every decision visible,
    same principle as the autofocus + platesolve engines."""
    if from_filter is not None and to_filter.upper() == from_filter.upper():
        return FocusJumpDecision(
            action="no_change",
            reason=f"filter unchanged ({to_filter})",
        )

    table = FilterOffsetTable()
    ref_filt = table.reference_filter()
    target = to_filter.upper()

    # Reference filter -> always AF (this is our calibration baseline)
    if target == ref_filt:
        return FocusJumpDecision(
            action="autofocus",
            reason=(f"target is reference filter {ref_filt}; "
                      "AF establishes the baseline step"),
        )

    # Non-reference: need both an offset entry AND a current reference
    # entry to compute the jump target
    target_entry = table.get(target)
    ref_entry = table.get(ref_filt)
    if target_entry is None or ref_entry is None:
        missing = []
        if target_entry is None:
            missing.append(f"offset for {target}")
        if ref_entry is None:
            missing.append(f"reference baseline {ref_filt}")
        return FocusJumpDecision(
            action="autofocus",
            reason=("missing data: " + ", ".join(missing)
                      + " — running AF to populate the table"),
        )

    # Staleness check — old measurements may not reflect current state
    if not target_entry.is_fresh() or not ref_entry.is_fresh():
        return FocusJumpDecision(
            action="autofocus",
            reason=(f"offset table stale (>{OFFSET_FRESHNESS_DAYS}d) — "
                      "AF refreshes the measurement"),
        )

    # Same reference filter? (Operator may have changed the reference
    # since last record.)
    if target_entry.reference_filter != ref_filt:
        return FocusJumpDecision(
            action="autofocus",
            reason=(f"{target} offset was measured vs "
                      f"{target_entry.reference_filter}, but current "
                      f"reference is {ref_filt} — re-AFing"),
        )

    # All good — jump
    target_step = ref_entry.offset_steps + target_entry.offset_steps
    return FocusJumpDecision(
        action="jump",
        target_step=int(target_step),
        offset_applied=int(target_entry.offset_steps),
        reason=(f"applying offset {target_entry.offset_steps:+d} steps "
                  f"({target} vs {ref_filt}); recorded "
                  f"{(datetime.utcnow() - target_entry.recorded_at).days}d ago"),
    )
