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
