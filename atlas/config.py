"""ATLAS configuration.

Two layers:

1. **Settings** (``get_settings()``): server-process configuration loaded from
   environment variables and process startup. Things like the install path,
   the database URL, the log level. Read-only after process start.

2. **SiteConfig** / **EquipmentProfile** (``get_site_config()``,
   ``get_equipment_profile()``): user-editable observatory configuration
   loaded from the database. Things like site latitude/longitude, NINA host,
   filter list. May change at runtime when the operator edits Setup.

The installer creates ``C:\\ATLAS`` and writes nothing to it that requires
human editing. All user-editable configuration lives in the database (so
it's encrypted, backed up with the DB, and never gets out of sync with
the schema).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---- Settings (process-level) -----------------------------------------------

class Settings(BaseSettings):
    """Process-level configuration. Read once at startup."""

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_",
        env_file=None,  # no .env in the new design; everything in DB
        extra="ignore",
    )

    # Paths --------------------------------------------------------------------
    install_root: Path = Field(default=Path(r"C:\ATLAS"))
    data_dir: Path = Field(default=Path(r"C:\ATLAS\data"))
    frames_dir: Path = Field(default=Path(r"C:\ATLAS\data\frames"))
    references_dir: Path = Field(default=Path(r"C:\ATLAS\data\references"))
    reports_dir: Path = Field(default=Path(r"C:\ATLAS\data\reports"))
    logs_dir: Path = Field(default=Path(r"C:\ATLAS\data\logs"))
    catalogs_dir: Path = Field(default=Path(r"C:\ATLAS\catalogs"))
    dashboard_dir: Path = Field(default=Path(r"C:\ATLAS\dashboard"))

    # Database -----------------------------------------------------------------
    database_url: str = Field(default="sqlite:///C:/ATLAS/data/atlas.db")

    # Server -------------------------------------------------------------------
    server_host: str = Field(default="0.0.0.0")
    server_port: int = Field(default=5000)

    # Logging ------------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_to_file: bool = Field(default=True)

    # Claude API model (overridable for testing) -------------------------------
    claude_model: str = Field(default="claude-sonnet-4-6")
    claude_max_tokens: int = Field(default=4096)

    # Mode ---------------------------------------------------------------------
    simulation_mode: bool = Field(default=False)

    def ensure_directories(self) -> None:
        """Create all required runtime directories."""
        for p in (
            self.data_dir, self.frames_dir, self.references_dir,
            self.reports_dir, self.logs_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_directories()
    return s


def is_simulation_mode() -> bool:
    """Whether simulation mode is active. Resolution order:

      1. If the ATLAS_SIMULATION_MODE env var is set (i.e., the
         Settings.simulation_mode field was loaded as True from env),
         it wins. Use this for boot-time overrides / CI / dev.
      2. Otherwise read the system_flags table (set via the Setup tab
         toggle). Default False.

    This lets ops choose: env-var control for headless servers, GUI
    toggle for interactive operators."""
    # Env-var override (always wins when True)
    if os.environ.get("ATLAS_SIMULATION_MODE", "").lower() in ("1", "true", "yes", "on"):
        return True
    # DB toggle — defer the import so this module stays importable from
    # the DB layer itself (avoids circular import on first init).
    try:
        from atlas.db.managers import ConfigManager
        flags = ConfigManager.get_system_flags()
        return bool(flags.simulation_mode)
    except Exception:
        return False


# ---- Site & equipment (DB-backed, runtime-editable) -------------------------

# These accessors defer to atlas.db.managers.ConfigManager once the DB is up.
# Defining the shape here keeps the import graph clean: agents and workflows
# import from atlas.config, never from atlas.db directly for these.

class SiteConfig(dict):
    """Observatory site configuration. Backed by a row in the `site_config`
    table. Treated as a dict-like for ease of use.

    Required keys (populated by Setup Wizard):
        latitude, longitude, elevation_m, timezone,
        observatory_name, observatory_code (optional MPC code)
    """


class EquipmentProfile(dict):
    """Equipment configuration. Backed by `equipment_profile` table.

    Required keys (populated by Setup Wizard):
        camera_type: "OSC" | "MONO"
        filters: list[str]                         # mono only
        focal_length_mm: float
        aperture_mm: float
        pixel_size_um: float
        pixel_scale_arcsec: float                  # may be auto-derived
        nina_host: str
        nina_port: int
        phd2_host: str
        phd2_port: int
        roof_mode: "nina" | "custom" | "manual"
        mount_supports_nonsidereal: bool
    """
