"""Pre-flight checklist (Round 4 #23).

Before commanding ``roof open``, the Operator agent runs this checklist.
Every check has a name, a callable, and a severity for failure.

Operator may override individual checks if the human explicitly approves.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Union

CheckFn = Callable[[], Union[bool, Awaitable[bool]]]


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str | None = None
    severity: str = "critical"   # "critical" blocks; "warning" advises


@dataclass
class PreflightResult:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed or c.severity != "critical" for c in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


class PreflightChecklist:
    """A composable preflight checklist.

    Add checks via ``add(name, fn, severity)``. ``run()`` executes all checks
    (sync or async) and returns a PreflightResult.
    """

    def __init__(self) -> None:
        self._checks: list[tuple[str, CheckFn, str]] = []

    def add(self, name: str, fn: CheckFn, *, severity: str = "critical") -> None:
        self._checks.append((name, fn, severity))

    async def run(self) -> PreflightResult:
        import asyncio
        result = PreflightResult()
        for name, fn, severity in self._checks:
            try:
                v = fn()
                if asyncio.iscoroutine(v):
                    v = await v
                passed = bool(v)
                result.checks.append(CheckResult(name, passed, severity=severity))
            except Exception as e:
                result.checks.append(CheckResult(name, False,
                                                   detail=str(e),
                                                   severity=severity))
        return result


# ---- Standard check builders -----------------------------------------------

def check_disk_free(path: Path | str, min_gb: float) -> CheckFn:
    def _check() -> bool:
        total, used, free = shutil.disk_usage(str(path))
        return free >= min_gb * 1_000_000_000
    return _check


def check_calibration_fresh(latest_dt: datetime | None, max_age_days: int) -> CheckFn:
    def _check() -> bool:
        if latest_dt is None:
            return False
        return datetime.utcnow() - latest_dt <= timedelta(days=max_age_days)
    return _check


# --- Hardware-equipment check builders ---------------------------------------
# These compose with a NinaClient (or FakeNina) — pass the right .info() coroutine.

def check_nina_equipment(info_coro_factory) -> CheckFn:
    """Generic "is this equipment online via NINA" check.

    Pass a zero-arg callable that returns a coroutine yielding the equipment
    info dict. Returns True iff the call succeeds AND ``info["connected"]``
    is truthy.
    """
    async def _check() -> bool:
        try:
            info = await info_coro_factory()
            return bool(info.get("connected"))
        except Exception:
            return False
    return _check


def check_focuser_ready(nina) -> CheckFn:
    """Focuser preflight: connected, and position within sensible bounds.

    The ZWO EAF needs to be online and at a non-extreme position before we
    can run a V-curve autofocus — being stuck at min or max means the EAF
    couldn't reach focus and the operator should investigate.
    """
    async def _check() -> bool:
        try:
            info = await nina.focuser_info()
        except Exception:
            return False
        if not info.get("connected"):
            return False
        pos = info.get("position")
        max_pos = info.get("max_position") or 0
        if pos is None or max_pos <= 0:
            return True   # connected but no bounds reported — accept
        # Reject if pinned at either end (likely hit a hard stop)
        margin = max(50, int(max_pos * 0.01))
        return margin < pos < (max_pos - margin)
    return _check


def check_camera_cooled(nina, setpoint_c: float, tolerance_c: float = 1.0) -> CheckFn:
    async def _check() -> bool:
        try:
            info = await nina.camera_info()
        except Exception:
            return False
        if not info.get("connected"):
            return False
        t = info.get("temperature")
        if t is None:
            return True   # camera connected but no temp readout
        return abs(t - setpoint_c) <= tolerance_c
    return _check


# ============================================================================
# Comprehensive session-readiness pre-flight
# ============================================================================

@dataclass
class Gate:
    """One pre-flight gate's status — what the dashboard's Session Readiness
    panel renders for each row."""
    name: str         # short slug — "weather", "hardware", "calibration", ...
    label: str        # human label — "Weather", "Hardware (NINA + PHD2)", ...
    status: str       # "ok" | "warning" | "critical" | "missing" | "unknown"
    message: str      # one-line current state in human English
    actionable: bool = True   # can the user fix this? (False = inevitable, e.g. dark window)

    def to_jsonable(self) -> dict:
        return {"name": self.name, "label": self.label,
                "status": self.status, "message": self.message,
                "actionable": self.actionable}


@dataclass
class SessionPreflight:
    """Aggregated readiness verdict for tonight's session, with per-gate
    breakdown. Drives the Session Readiness dashboard panel + the
    Operator's broadcast verdict."""
    assessed_at: str
    verdict: str          # "GO" | "WAITING" | "CAUTION" | "NO-GO" | "UNKNOWN"
    reason: str           # one-line summary
    next_action: str      # what the system / operator should do next
    gates: list[Gate] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        return {"assessed_at": self.assessed_at,
                "verdict": self.verdict,
                "reason": self.reason,
                "next_action": self.next_action,
                "gates": [g.to_jsonable() for g in self.gates]}


# ----- Severity ranking -----------------------------------------------------

_GATE_RANK = {"ok": 0, "unknown": 1, "warning": 2,
                "missing": 3, "critical": 4}


def _worst_status(*statuses: str) -> str:
    return max(statuses, key=lambda s: _GATE_RANK.get(s, 0))


# ----- Individual gate checks ----------------------------------------------

def _gate_weather() -> Gate:
    from atlas.agents.state import get_state
    a = get_state().get_assessment()
    if a is None:
        return Gate("weather", "Weather", "unknown",
                     "Critic hasn't produced an assessment yet.")
    sev = a.overall_severity  # "ok" | "warning" | "critical"
    return Gate("weather", "Weather", sev, a.summary or "—")


def _gate_hardware() -> Gate:
    """Best-effort hardware probe. Uses the cached snapshot from the
    Tonight tab route (already TTL-cached + timeout-bounded so we don't
    block here)."""
    from atlas.config import get_settings
    settings = get_settings()
    if settings.simulation_mode:
        return Gate("hardware", "Hardware (sim)", "ok",
                     "Simulation mode — fake hardware reports all green.",
                     actionable=False)
    try:
        from atlas.api.routes import _HARDWARE_SNAPSHOT_CACHE
        snap = _HARDWARE_SNAPSHOT_CACHE.get("data")
    except Exception:
        snap = None
    if not snap:
        return Gate("hardware", "Hardware (NINA + PHD2)", "unknown",
                     "No hardware snapshot yet — Tonight tab populates this.")
    bad = [k for k, v in snap.items()
           if not v.get("connected") and v.get("status") != "n/a"]
    if not bad:
        return Gate("hardware", "Hardware (NINA + PHD2)", "ok",
                     "All devices report connected.")
    if "guiding" in bad and len(bad) == 1:
        # Guiding offline pre-session is normal
        return Gate("hardware", "Hardware (NINA + PHD2)", "warning",
                     "Guiding (PHD2) not connected — required once session starts.")
    return Gate("hardware", "Hardware (NINA + PHD2)", "critical",
                 f"Disconnected: {', '.join(bad)}")


def _gate_calibration() -> Gate:
    """Recent calibration masters (bias + dark + flat) within the
    configured freshness window."""
    from atlas.db.managers import ConfigManager
    from atlas.db.models import CalibrationMaster
    from atlas.db.session import get_session
    from datetime import datetime, timedelta
    retention = ConfigManager.get_retention()
    days = int(retention.calibration_freshness_days or 7)
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as s:
        fresh_kinds = {row[0] for row in s.query(CalibrationMaster.kind)
                                              .filter(CalibrationMaster.created_at >= cutoff)
                                              .distinct().all()}
    have_bias = "bias" in fresh_kinds
    have_dark = "dark" in fresh_kinds
    have_flat = "flat" in fresh_kinds
    n_have = sum([have_bias, have_dark, have_flat])
    if n_have == 3:
        return Gate("calibration", "Calibration library", "ok",
                     f"All three master types within {days} days.")
    if n_have == 0:
        return Gate("calibration", "Calibration library", "missing",
                     f"No fresh calibration masters (window: {days} days). "
                     "Capture darks/flats before imaging.")
    missing = [k for k, p in [("bias", have_bias), ("dark", have_dark),
                                ("flat", have_flat)] if not p]
    return Gate("calibration", "Calibration library", "warning",
                 f"Missing fresh: {', '.join(missing)} (window: {days} days)")


def _gate_plan() -> Gate:
    from atlas.agents.state import get_state
    plan = get_state().get_tonight_plan()
    if plan is None:
        return Gate("plan", "Tonight's plan", "missing",
                     "Planner hasn't produced a plan yet.")
    n = len(plan.get("visible_targets") or [])
    fallback = plan.get("fallback_to_catalog")
    if n == 0:
        return Gate("plan", "Tonight's plan", "warning",
                     "No targets visible tonight (all below horizon or no coords).")
    source = "seasonal showcase" if fallback else "active campaigns"
    return Gate("plan", "Tonight's plan", "ok",
                 f"{n} target(s) ready from {source}.")


def _gate_disk() -> Gate:
    import shutil
    from atlas.config import get_settings
    from atlas.db.managers import ConfigManager
    settings = get_settings()
    retention = ConfigManager.get_retention()
    warn_pct = float(retention.alert_warn_pct or 80.0)
    crit_pct = float(retention.alert_block_pct or 95.0)
    try:
        total, used, free = shutil.disk_usage(str(settings.data_dir))
    except Exception as e:
        return Gate("disk", "Disk space", "unknown", f"Cannot read disk: {e}")
    used_pct = (used / total) * 100.0 if total else 0
    free_gb = free / (1024 ** 3)
    if used_pct >= crit_pct:
        return Gate("disk", "Disk space", "critical",
                     f"{used_pct:.1f}% used ({free_gb:.0f} GB free) — above critical {crit_pct:.0f}%")
    if used_pct >= warn_pct:
        return Gate("disk", "Disk space", "warning",
                     f"{used_pct:.1f}% used ({free_gb:.0f} GB free) — above warn {warn_pct:.0f}%")
    return Gate("disk", "Disk space", "ok",
                 f"{free_gb:.0f} GB free ({used_pct:.1f}% used)")


def _gate_vault() -> Gate:
    try:
        from atlas.security import get_vault
        v = get_vault()
    except Exception as e:
        return Gate("vault", "Credential vault", "unknown", str(e))
    if not v.is_initialised:
        return Gate("vault", "Credential vault", "missing",
                     "Vault not initialised. Open Setup → Master Password.")
    if not v.is_unlocked:
        return Gate("vault", "Credential vault", "critical",
                     "Vault locked. Agents can't reach the Claude API. Open Setup to unlock.",
                     actionable=True)
    return Gate("vault", "Credential vault", "ok", "Unlocked.")


def _gate_api_health() -> Gate:
    """Any agent in safe-autonomous mode → Claude API trouble."""
    try:
        from atlas.agents.coordinator import get_coordinator
        statuses = get_coordinator().status()
    except Exception:
        return Gate("api", "Claude API health", "unknown",
                     "Coordinator not ready.")
    in_safe = [n for n, s in statuses.items() if s.get("safe_mode")]
    not_running = [n for n, s in statuses.items() if not s.get("running")]
    if not_running:
        return Gate("api", "Agent processes", "critical",
                     f"Not running: {', '.join(not_running)}")
    if in_safe:
        return Gate("api", "Claude API health", "warning",
                     f"In safe-autonomous mode: {', '.join(in_safe)}")
    return Gate("api", "Claude API health", "ok",
                 "All 5 agents reachable.")


def _gate_dark_window() -> Gate:
    """Are we in (or approaching) tonight's dark window?

    Uses sun altitude at the configured site:
      sun ≤ -12°   → in darkness, OK to image
      sun ≤ -6°    → civil twilight, warning (acceptable for setup)
      otherwise    → daylight; this is normal mid-day, not an error
    Always returns actionable=False because time-of-day is inevitable."""
    from atlas.db.managers import ConfigManager
    from atlas.astronomy import sun_altitude, night_window
    from atlas.units import to_eastern
    site = ConfigManager.get_site()
    if site is None:
        return Gate("time", "Dark window", "unknown",
                     "No site config — can't compute sun position.",
                     actionable=True)
    lat, lon = float(site.latitude), float(site.longitude)
    try:
        alt = sun_altitude(lat, lon, datetime.utcnow())
    except Exception as e:
        return Gate("time", "Dark window", "unknown",
                     f"Sun position failed: {e}", actionable=True)
    if alt <= -12.0:
        return Gate("time", "Dark window", "ok",
                     f"Sun at {alt:.1f}° — fully dark.", actionable=False)
    if alt <= -6.0:
        return Gate("time", "Dark window", "warning",
                     f"Sun at {alt:.1f}° — civil twilight (setup OK, imaging marginal).",
                     actionable=False)
    # Sun up. Find when dusk happens.
    nw = night_window(lat, lon, datetime.utcnow(), altitude_deg=-12.0)
    if nw:
        dusk, dawn = nw
        local = to_eastern(dusk).strftime("%H:%M %Z")
        mins = max(0, int((dusk - datetime.utcnow()).total_seconds() / 60))
        return Gate("time", "Dark window", "warning",
                     f"Sun at {alt:.1f}° — astronomical dusk at {local} (in {mins} min).",
                     actionable=False)
    return Gate("time", "Dark window", "warning",
                 f"Sun at {alt:.1f}° — no dark window in next 36h.",
                 actionable=False)


# ----- Top-level entry point ------------------------------------------------

async def run_session_preflight() -> SessionPreflight:
    """Run every session-readiness gate and aggregate into a verdict.

    Verdict rules:
      NO-GO   : any gate is "critical" or "missing" (and actionable)
      WAITING : everything else is OK, but the sun is up (warning on time gate)
      CAUTION : at least one warning, nothing critical/missing
      GO      : every gate is "ok"
      UNKNOWN : every gate is "unknown" (very early bootstrap)
    """
    gates = [
        _gate_vault(),
        _gate_weather(),
        _gate_hardware(),
        _gate_plan(),
        _gate_calibration(),
        _gate_disk(),
        _gate_api_health(),
        _gate_dark_window(),
    ]

    # Aggregate
    ranks = [_GATE_RANK.get(g.status, 0) for g in gates]
    worst = _worst_status(*[g.status for g in gates])
    time_gate = next((g for g in gates if g.name == "time"), None)
    actionable_blockers = [g for g in gates
                            if g.status in ("critical", "missing")
                            and g.actionable]

    if all(s == "unknown" for s in [g.status for g in gates]):
        verdict, reason = "UNKNOWN", "Pre-flight just started — gates initialising."
        next_action = "Wait for the Critic and Planner to produce their first ticks."
    elif actionable_blockers:
        verdict = "NO-GO"
        names = ", ".join(g.label for g in actionable_blockers)
        reason = f"Blocked by: {names}"
        next_action = "Fix the failing gate(s) before opening the roof."
    elif time_gate and time_gate.status != "ok":
        # Dark window not open yet — every other check might be fine
        non_time_worst = _worst_status(*[g.status for g in gates if g.name != "time"])
        if non_time_worst in ("critical", "missing"):
            verdict = "NO-GO"
            reason = "Conditions blocked AND awaiting dark window."
            next_action = "Resolve blockers; reassess at dusk."
        elif non_time_worst == "warning":
            verdict = "CAUTION"
            reason = f"{time_gate.message} Other gate(s) report warnings."
            next_action = "Address warnings before dusk."
        else:
            verdict = "WAITING"
            reason = time_gate.message
            next_action = "All other gates ready — start when dark window opens."
    elif worst == "warning":
        verdict = "CAUTION"
        warns = [g.label for g in gates if g.status == "warning"]
        reason = f"Warning(s): {', '.join(warns)}"
        next_action = "Proceed with caution; consider deferring."
    else:
        verdict = "GO"
        reason = "All gates green."
        next_action = "Ready to start tonight's session."

    return SessionPreflight(
        assessed_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        verdict=verdict, reason=reason, next_action=next_action,
        gates=gates,
    )
