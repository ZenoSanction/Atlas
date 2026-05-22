"""Autofocus orchestrator.

NINA owns the actual V-curve autofocus run (it knows the focuser, has
the star-detection code, finds the HFR minimum). ATLAS owns the
*policy* — when to ask NINA to refocus.

Five triggers, each independently configurable per workflow:

  1. session_start    — first frame of a session (always, no debate)
  2. filter_change    — different filters have different focal lengths
                        on mono setups; OSC ignores this trigger
  3. temp_delta_c     — ambient/CCD temperature shift since last AF
                        (typical default: 2 °C)
  4. time_elapsed_min — fallback: hourly refocus during long sessions
  5. hfr_degradation  — current HFR > REF * factor (typical 1.25x)

Every decision (fire OR skip with reason) is logged to DecisionManager
and broadcast over the bus so the human can see exactly when and why
ATLAS refocused — central to building trust in the autonomous pipeline.

The actual NINA call is a one-line invocation that's a placeholder
in sim mode (sleeps + returns success) and swaps to the real
``nina.run_autofocus()`` endpoint on bench day.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from atlas.logging_setup import get_logger

log = get_logger("capture.autofocus")


# Default thresholds. Per-workflow overrides live on AutofocusPolicy
# (atlas/workflows/base.py) — the workflow module configures the
# policy and the orchestrator reads from it. These are sensible
# defaults if a workflow doesn't override.
DEFAULT_TEMP_DELTA_C = 2.0
DEFAULT_TIME_ELAPSED_MIN = 60.0
DEFAULT_HFR_FACTOR = 1.25


@dataclass
class AutofocusContext:
    """Inputs to the decision engine. The capture sequence assembles
    this dict before every potential decision point (start-of-session,
    filter change, before each new frame)."""
    session_started: bool = False           # True on the very first frame
    filter_changed: bool = False            # True when last frame was a
                                              # different filter
    current_filter: Optional[str] = None
    current_temp_c: Optional[float] = None  # ambient / CCD temp now
    last_af_temp_c: Optional[float] = None  # ambient at last AF
    last_af_at: Optional[datetime] = None   # last AF wall-clock
    current_hfr: Optional[float] = None     # most recent frame HFR
    reference_hfr: Optional[float] = None   # HFR right after last AF
    is_mono: bool = False                   # filter change matters only on mono


@dataclass
class AutofocusDecision:
    """Outcome of a should_fire() call."""
    should_fire: bool
    trigger: str                # "session_start" | "filter_change" | ...
    reason: str                 # human-readable, lands on dashboard
    skip_because: str = ""      # populated when should_fire is False


class AutofocusDecisionEngine:
    """Pure-logic: given a context + a policy, decide whether to
    refocus. Stateless — every decision is derived from the inputs
    so the same inputs always produce the same decision (testable)."""

    def __init__(self, *,
                 trigger_on_session_start: bool = True,
                 trigger_on_filter_change: bool = True,
                 temp_delta_c: float = DEFAULT_TEMP_DELTA_C,
                 time_elapsed_min: float = DEFAULT_TIME_ELAPSED_MIN,
                 hfr_factor: float = DEFAULT_HFR_FACTOR) -> None:
        self.trigger_on_session_start = trigger_on_session_start
        self.trigger_on_filter_change = trigger_on_filter_change
        self.temp_delta_c = float(temp_delta_c)
        self.time_elapsed_min = float(time_elapsed_min)
        self.hfr_factor = float(hfr_factor)

    def should_fire(self, ctx: AutofocusContext) -> AutofocusDecision:
        # 1. Session start — fires unconditionally (unless workflow
        #    opts out, e.g. exoplanet locked-focus mode)
        if ctx.session_started and self.trigger_on_session_start:
            return AutofocusDecision(
                should_fire=True, trigger="session_start",
                reason="initial focus on session start",
            )

        # 2. Filter change — only meaningful on mono setups
        if ctx.filter_changed and self.trigger_on_filter_change:
            if ctx.is_mono:
                return AutofocusDecision(
                    should_fire=True, trigger="filter_change",
                    reason=(f"filter changed to {ctx.current_filter}; "
                              "mono setup needs per-filter focus"),
                )
            else:
                # OSC: filter change is meaningless. Note in case the
                # operator's looking at why we didn't refocus.
                return AutofocusDecision(
                    should_fire=False, trigger="filter_change",
                    reason="filter changed", skip_because="OSC setup",
                )

        # 3. Temperature delta
        if (ctx.current_temp_c is not None
              and ctx.last_af_temp_c is not None):
            delta = abs(ctx.current_temp_c - ctx.last_af_temp_c)
            if delta >= self.temp_delta_c:
                return AutofocusDecision(
                    should_fire=True, trigger="temp_delta",
                    reason=(f"temperature shifted {delta:.1f}°C "
                              f"(≥ {self.temp_delta_c:.1f}°C threshold) "
                              f"since last autofocus"),
                )

        # 4. Time elapsed
        if ctx.last_af_at is not None:
            mins = (datetime.utcnow() - ctx.last_af_at).total_seconds() / 60.0
            if mins >= self.time_elapsed_min:
                return AutofocusDecision(
                    should_fire=True, trigger="time_elapsed",
                    reason=(f"{mins:.0f} min since last autofocus "
                              f"(≥ {self.time_elapsed_min:.0f} min interval)"),
                )

        # 5. HFR degradation
        if (ctx.current_hfr is not None
              and ctx.reference_hfr is not None
              and ctx.reference_hfr > 0):
            ratio = ctx.current_hfr / ctx.reference_hfr
            if ratio >= self.hfr_factor:
                return AutofocusDecision(
                    should_fire=True, trigger="hfr_degradation",
                    reason=(f"HFR degraded from {ctx.reference_hfr:.2f} "
                              f"to {ctx.current_hfr:.2f} "
                              f"(ratio {ratio:.2f} ≥ {self.hfr_factor})"),
                )

        return AutofocusDecision(
            should_fire=False, trigger="(none)",
            reason="all triggers below threshold",
            skip_because="no trigger met",
        )


# ---- Execution wrapper (placeholder for the real NINA endpoint) ----------

async def run_autofocus(nina, *, simulation: bool = False,
                          timeout_s: float = 180.0) -> dict:
    """Fire one autofocus run. Returns a result dict with:
        {ok, hfr_min, focuser_position, elapsed_s, ...}

    Real mode: calls NINA's autofocus endpoint (NINA Advanced API
    has /api/equipment/focuser/auto-focus or similar). The exact
    endpoint shape depends on your NINA version — wire it on
    bench day.

    Sim mode: sleeps briefly, returns synthetic results so the
    decision engine + capture-sequence integration can be exercised
    end-to-end without real hardware."""
    started = time.monotonic()
    if simulation:
        # Realistic autofocus runs are 30-90 seconds. Sim shrinks to ~1.
        await asyncio.sleep(1.0)
        return {
            "ok": True,
            "hfr_min": 1.85,           # synthetic; real value comes from NINA
            "focuser_position": 15321,
            "elapsed_s": round(time.monotonic() - started, 1),
            "trigger_used": "sim",
            "note": "simulation — no real autofocus performed",
        }
    # Real path — replace with the actual NINA autofocus call. Stub
    # for now so the orchestration is testable. On bench day this
    # becomes one line: `result = await nina.run_autofocus(...)`.
    log.warning("run_autofocus called in real mode but NINA endpoint "
                 "not yet wired. Sky-time prerequisite.")
    try:
        await asyncio.wait_for(asyncio.sleep(2.0), timeout=timeout_s)
        return {
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 1),
            "error": "real autofocus endpoint not wired yet",
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 1),
            "error": f"autofocus timed out after {timeout_s:.0f}s",
        }
