"""Boot-time checks for external-tool availability.

Surface known-bad configuration loudly at startup so the operator
finds out about missing or moved binaries before a session needs
them mid-night. Each check sets a system flag the dashboard can
read so the Setup tab shows green/yellow/red status for each
external dependency.

Currently checks:
  - ASTAP (plate solver)
  - Siril (stacking — future)

Easy to add more (HOTPANTS, sextractor, etc.) as the pipeline grows.
"""
from __future__ import annotations

from pathlib import Path

from atlas.agents.state import get_state


# Public so dashboard / API can read the same dict
EXTERNAL_TOOL_STATUS: dict[str, dict] = {}


def check_external_tools(log) -> dict[str, dict]:
    """Run all external-tool checks. Returns the status dict and also
    parks it on the shared state ring buffer + EXTERNAL_TOOL_STATUS
    module-level for any consumer.

    Each entry: {tool_name: {"configured": bool, "path": str|None,
                              "available": bool, "detail": str}}
    Non-fatal: missing tools log a warning but the server still starts."""
    from atlas.db.managers import ConfigManager
    equip = ConfigManager.get_equipment()
    out: dict[str, dict] = {}

    # ---- ASTAP ----
    astap_path = getattr(equip, "astap_path", None) if equip else None
    out["astap"] = _check_binary(
        "astap", astap_path,
        purpose="plate solver — required for autonomous mount sync after slew",
        log=log,
    )

    # ---- Siril (placeholder for future) ----
    siril_path = getattr(equip, "siril_path", None) if equip else None
    out["siril"] = _check_binary(
        "siril", siril_path,
        purpose="stacking pipeline — currently optional",
        log=log,
    )

    EXTERNAL_TOOL_STATUS.clear()
    EXTERNAL_TOOL_STATUS.update(out)
    return out


def _check_binary(name: str, configured_path: str | None,
                    *, purpose: str, log) -> dict:
    """One binary check. Returns a status dict + logs appropriately."""
    if not configured_path:
        log.info("Startup check: %s not configured (skipping). "
                  "Configure in Setup if you need it (%s).",
                  name.upper(), purpose)
        return {"configured": False, "path": None, "available": False,
                "detail": "not configured"}
    p = Path(configured_path)
    if p.exists() and p.is_file():
        log.info("Startup check: %s found at %s", name.upper(), p)
        return {"configured": True, "path": str(p), "available": True,
                "detail": "OK"}
    log.warning("Startup check: %s configured but NOT FOUND at %s. "
                 "%s will be unavailable until the path is corrected.",
                 name.upper(), p, purpose)
    return {"configured": True, "path": str(p), "available": False,
            "detail": f"path configured but binary not found at {p}"}


def get_tool_status() -> dict[str, dict]:
    """Read the cached status. Empty until check_external_tools runs."""
    return dict(EXTERNAL_TOOL_STATUS)
