"""Pydantic schemas for the HTTP API."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    version: str
    simulation_mode: bool
    agents: dict


class SetupStatus(BaseModel):
    """Tells the dashboard what's left to configure."""
    vault_initialised: bool
    site_configured: bool
    equipment_configured: bool
    anthropic_key_set: bool
    notifications_configured: bool


class InitVaultRequest(BaseModel):
    password: str = Field(..., min_length=8)


class UnlockVaultRequest(BaseModel):
    password: str


class SetCredentialRequest(BaseModel):
    key: str
    value: str
    description: Optional[str] = None


class SiteConfigSchema(BaseModel):
    latitude: float
    longitude: float
    elevation_m: float = 0.0
    timezone: str
    observatory_name: str
    observatory_code: Optional[str] = None
    horizon_az_min: float = 0.0
    horizon_az_max: float = 360.0
    horizon_alt_min: float = 20.0


class EquipmentSchema(BaseModel):
    camera_type: str  # OSC | MONO
    filters: Optional[list[str]] = None
    sensor_pixel_size_um: float
    pixel_scale_arcsec: Optional[float] = None
    focal_length_mm: float
    aperture_mm: float
    nina_host: str = "localhost"
    nina_port: int = 1888
    phd2_host: str = "localhost"
    phd2_port: int = 4400
    astap_path: Optional[str] = None
    siril_path: Optional[str] = None
    roof_mode: str = "manual"
    roof_driver_module: Optional[str] = None
    mount_supports_nonsidereal: bool = False
    cooling_setpoint_c: float = -10.0
    warmup_ramp_c_per_min: float = 5.0


class CampaignCreate(BaseModel):
    name: str
    workflow: str   # WorkflowKind value
    priority: int = 50
    cadence: Optional[str] = None
    scientific_context: Optional[str] = None
    target_names: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    safe_mode: bool


class OperatorCommand(BaseModel):
    command: str
    params: dict = Field(default_factory=dict)


class SubmissionAction(BaseModel):
    action: str  # "approve" | "reject"
    notes: Optional[str] = None
    reason: Optional[str] = None
