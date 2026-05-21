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

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---- Settings (process-level) -----------------------------------------------

# Default install root. Override with the ATLAS_INSTALL_ROOT env var to
# put the whole install on a different drive (e.g. D:\ATLAS for a
# big data drive). All sub-paths derive from this unless individually
# overridden.
_DEFAULT_INSTALL_ROOT = Path(r"C:\ATLAS")


class Settings(BaseSettings):
    """Process-level configuration. Read once at startup.

    Single switch: set ATLAS_INSTALL_ROOT and every sub-path + the
    database URL move with it. Individual sub-paths can still be
    overridden via their own env vars (ATLAS_DATA_DIR, ATLAS_FRAMES_DIR,
    ATLAS_DATABASE_URL, etc.) when you want e.g. the install on SSD but
    captured frames on a big spinning drive."""

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_",
        env_file=None,  # no .env in the new design; everything in DB
        extra="ignore",
    )

    # Paths --------------------------------------------------------------------
    install_root: Path = Field(default=_DEFAULT_INSTALL_ROOT)
    # The rest are optional — if unset, derived from install_root below.
    data_dir: Path | None = Field(default=None)
    frames_dir: Path | None = Field(default=None)
    references_dir: Path | None = Field(default=None)
    reports_dir: Path | None = Field(default=None)
    logs_dir: Path | None = Field(default=None)
    catalogs_dir: Path | None = Field(default=None)
    dashboard_dir: Path | None = Field(default=None)
    masters_dir: Path | None = Field(default=None)
    morning_reports_dir: Path | None = Field(default=None)
    submissions_dir: Path | None = Field(default=None)
    sessions_dir: Path | None = Field(default=None)

    # Database -----------------------------------------------------------------
    database_url: str | None = Field(default=None)

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

    @model_validator(mode="after")
    def _derive_paths(self):
        """Fill in any unset sub-paths from install_root. Lets the operator
        set just ATLAS_INSTALL_ROOT=D:\\ATLAS and have everything follow."""
        root = self.install_root
        if self.data_dir is None:
            self.data_dir = root / "data"
        if self.frames_dir is None:
            self.frames_dir = self.data_dir / "frames"
        if self.references_dir is None:
            self.references_dir = self.data_dir / "references"
        if self.reports_dir is None:
            self.reports_dir = self.data_dir / "reports"
        if self.logs_dir is None:
            self.logs_dir = self.data_dir / "logs"
        if self.catalogs_dir is None:
            self.catalogs_dir = root / "catalogs"
        if self.dashboard_dir is None:
            self.dashboard_dir = root / "dashboard"
        if self.masters_dir is None:
            self.masters_dir = self.data_dir / "masters"
        if self.morning_reports_dir is None:
            self.morning_reports_dir = self.data_dir / "morning_reports"
        if self.submissions_dir is None:
            self.submissions_dir = self.data_dir / "submissions"
        if self.sessions_dir is None:
            self.sessions_dir = self.data_dir / "sessions"
        if self.database_url is None:
            # Use forward slashes — SQLAlchemy parses them on Windows too.
            db_path = (self.data_dir / "atlas.db").as_posix()
            self.database_url = f"sqlite:///{db_path}"
        return self

    def ensure_directories(self) -> None:
        """Create all required runtime directories."""
        for p in (
            self.data_dir, self.frames_dir, self.references_dir,
            self.reports_dir, self.logs_dir, self.masters_dir,
            self.morning_reports_dir, self.submissions_dir,
            self.sessions_dir,
        ):
            if p is not None:
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
