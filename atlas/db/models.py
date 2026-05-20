"""ATLAS database schema.

Domain organisation:

    Configuration        site_config, equipment_profile, credentials,
                         retention_policy, notification_config
    Identity             observer, version_info
    Astronomy            target, knowledge_thread, campaign, campaign_target
    Operations           session, alert, decision, agent_message
    Data                 frame, calibration_master, reference_frame,
                         stack_product
    Science              measurement, submission
    Storage              storage_event

Every measurement that could be submitted to MPC/AAVSO/TNS/NASA Exoplanet
Watch lives in ``measurement``, and the ``submission`` table tracks the
human-approval lifecycle: produced -> queued -> approved -> submitted
(or rejected).
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, JSON, LargeBinary,
    String, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from atlas.db.session import Base


# ============================================================================
# Enums
# ============================================================================

class SessionState(str, enum.Enum):
    PRE_SESSION   = "pre_session"
    NOMINAL       = "nominal"
    WARNING       = "warning"
    CRITICAL      = "critical"
    STANDBY_LIGHT = "standby_light"
    STANDBY_FULL  = "standby_full"
    SHUTDOWN      = "shutdown"
    COMPLETE      = "complete"


class AlertSeverity(str, enum.Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


class WorkflowKind(str, enum.Enum):
    ASTROMETRY    = "astrometry"     # Asteroid/comet
    PHOTOMETRY    = "photometry"     # Variable star
    EXOPLANET     = "exoplanet"      # Transit photometry
    TRANSIENT     = "transient"      # SN hunting
    PLANETARY     = "planetary"
    DEEPSKY       = "deepsky"        # Aesthetic + general


class CampaignStatus(str, enum.Enum):
    PROPOSED   = "proposed"          # Oracle/operator-suggested, awaiting approval
    ACTIVE     = "active"
    PAUSED     = "paused"
    COMPLETED  = "completed"
    CANCELLED  = "cancelled"


class FrameQuality(str, enum.Enum):
    A = "A"   # excellent
    B = "B"   # good, science-ready
    C = "C"   # marginal
    D = "D"   # discard candidate
    UNGRADED = "ungraded"


class SubmissionDestination(str, enum.Enum):
    MPC          = "mpc"             # Minor Planet Center
    AAVSO        = "aavso"
    TNS          = "tns"
    NASA_EO      = "nasa_exoplanet_watch"


class SubmissionStatus(str, enum.Enum):
    QUEUED     = "queued"           # awaiting operator review
    APPROVED   = "approved"         # operator approved, ready to send
    SUBMITTED  = "submitted"        # sent to external service
    ACK        = "acknowledged"     # external service confirmed receipt
    REJECTED   = "rejected"         # operator rejected
    FAILED     = "failed"           # submission error


class MeasurementKind(str, enum.Enum):
    ASTROMETRY  = "astrometry"
    PHOTOMETRY  = "photometry"
    TRANSIENT   = "transient_candidate"


class AgentName(str, enum.Enum):
    PLANNER   = "planner"
    CRITIC    = "critic"
    OPERATOR  = "operator"
    ARCHIVIST = "archivist"
    ORACLE    = "oracle"


class AgentMessageKind(str, enum.Enum):
    ALERT             = "alert"
    REVISION_REQUEST  = "revision_request"
    POST_SESSION      = "post_session_trigger"
    NEW_DATA          = "new_data_notification"
    CANDIDATE_TARGET  = "candidate_target_proposal"
    STATUS            = "status"
    DECISION          = "decision"
    OPERATOR_COMMAND  = "operator_command"


# ============================================================================
# Configuration tables
# ============================================================================

class SiteConfig(Base):
    """Single-row table holding observatory site config."""
    __tablename__ = "site_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    elevation_m: Mapped[float] = mapped_column(Float, default=0.0)
    timezone: Mapped[str] = mapped_column(String(64))
    observatory_name: Mapped[str] = mapped_column(String(128))
    observatory_code: Mapped[Optional[str]] = mapped_column(String(8))  # MPC code if assigned
    horizon_az_min: Mapped[float] = mapped_column(Float, default=0.0)
    horizon_az_max: Mapped[float] = mapped_column(Float, default=360.0)
    horizon_alt_min: Mapped[float] = mapped_column(Float, default=20.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)


class EquipmentProfile(Base):
    """Single-row table holding the active equipment profile."""
    __tablename__ = "equipment_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    camera_type: Mapped[str] = mapped_column(String(16))       # "OSC" | "MONO"
    filters: Mapped[Optional[dict]] = mapped_column(JSON)      # mono only: ["L","R","G","B","V","Ha"]
    sensor_pixel_size_um: Mapped[float] = mapped_column(Float)
    pixel_scale_arcsec: Mapped[Optional[float]] = mapped_column(Float)
    focal_length_mm: Mapped[float] = mapped_column(Float)
    aperture_mm: Mapped[float] = mapped_column(Float)
    nina_host: Mapped[str] = mapped_column(String(128), default="localhost")
    nina_port: Mapped[int] = mapped_column(Integer, default=1888)
    phd2_host: Mapped[str] = mapped_column(String(128), default="localhost")
    phd2_port: Mapped[int] = mapped_column(Integer, default=4400)
    astap_path: Mapped[Optional[str]] = mapped_column(String(512))
    siril_path: Mapped[Optional[str]] = mapped_column(String(512))
    roof_mode: Mapped[str] = mapped_column(String(16), default="manual")  # nina | custom | manual
    roof_driver_module: Mapped[Optional[str]] = mapped_column(String(256))
    mount_supports_nonsidereal: Mapped[bool] = mapped_column(Boolean, default=False)
    cooling_setpoint_c: Mapped[float] = mapped_column(Float, default=-10.0)
    warmup_ramp_c_per_min: Mapped[float] = mapped_column(Float, default=5.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)


class Credential(Base):
    """Encrypted credentials (API keys, submission tokens).

    The ``value_encrypted`` blob is AES-256-GCM ciphertext produced by
    ``atlas.security.CredentialVault.encrypt``. Never store plaintext here.
    """
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    value_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    description: Mapped[Optional[str]] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)


class RetentionPolicy(Base):
    """Storage retention. Single-row table."""
    __tablename__ = "retention_policy"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_subs_days: Mapped[int] = mapped_column(Integer, default=90)
    planetary_video_days: Mapped[int] = mapped_column(Integer, default=30)
    session_reports_days: Mapped[int] = mapped_column(Integer, default=-1)  # -1 = forever
    calibration_masters_days: Mapped[int] = mapped_column(Integer, default=-1)
    references_days: Mapped[int] = mapped_column(Integer, default=-1)
    alert_warn_pct: Mapped[float] = mapped_column(Float, default=80.0)
    alert_block_pct: Mapped[float] = mapped_column(Float, default=95.0)
    calibration_freshness_days: Mapped[int] = mapped_column(Integer, default=7)


class NotificationConfig(Base):
    """Notification (ntfy.sh) configuration."""
    __tablename__ = "notification_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    ntfy_server: Mapped[str] = mapped_column(String(256), default="https://ntfy.sh")
    ntfy_topic_credential_key: Mapped[Optional[str]] = mapped_column(String(64))
    notify_info: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_warning: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_critical: Mapped[bool] = mapped_column(Boolean, default=True)


class VersionInfo(Base):
    """Stamp the installer leaves so we know which version touched the DB."""
    __tablename__ = "version_info"

    id: Mapped[int] = mapped_column(primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(32))
    atlas_version: Mapped[str] = mapped_column(String(32))
    installed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WeatherThresholds(Base):
    """User-tunable weather safety thresholds. Single-row table. The Critic
    reads these on every standard-loop tick; the Setup tab edits them.
    Defaults match atlas.safety.thresholds.SafetyThresholds()."""
    __tablename__ = "weather_thresholds"

    id: Mapped[int] = mapped_column(primary_key=True)
    wind_speed_warn_ms: Mapped[float] = mapped_column(Float, default=6.7)
    wind_speed_critical_ms: Mapped[float] = mapped_column(Float, default=8.9)
    humidity_warn_pct: Mapped[float] = mapped_column(Float, default=85.0)
    humidity_critical_pct: Mapped[float] = mapped_column(Float, default=95.0)
    dew_margin_warn_c: Mapped[float] = mapped_column(Float, default=5.0)
    dew_margin_critical_c: Mapped[float] = mapped_column(Float, default=2.0)
    cloud_cover_warn_pct: Mapped[float] = mapped_column(Float, default=60.0)
    cloud_cover_critical_pct: Mapped[float] = mapped_column(Float, default=85.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)


class AgentMemory(Base):
    """Persistent memory facts the agents remember across restarts.

    Each row belongs to one agent (or the special bucket 'shared', which
    every agent can read). 'pinned' rows are auto-injected into the
    agent's system prompt on every chat call so the model never has to
    look them up. Non-pinned rows are stored and become accessible via
    the agent's recall() tool — they don't bloat every system prompt,
    but they're never lost."""
    __tablename__ = "agent_memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "planner" | "critic" | "operator" | "archivist" | "oracle" | "shared"
    agent: Mapped[str] = mapped_column(String(16), index=True)
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[Optional[list]] = mapped_column(JSON)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source: Mapped[str] = mapped_column(String(32), default="chat")  # chat|api|bootstrap
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                   index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                   onupdate=datetime.utcnow)


class AgentChatTurn(Base):
    """One side of a chat conversation with an agent. Loaded oldest-first
    into the model's message history so chats remain continuous across
    messages and across server restarts."""
    __tablename__ = "agent_chat_turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(16), index=True)
    role: Mapped[str] = mapped_column(String(16))   # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                   index=True)


# ============================================================================
# Astronomy
# ============================================================================

class Target(Base):
    """A target known to ATLAS. Astronomical objects, asteroid designations,
    transient candidate fields — everything resolvable to a sky position.
    """
    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True, unique=True)
    object_type: Mapped[str] = mapped_column(String(32))    # galaxy, asteroid, var_star, etc.
    ra_deg: Mapped[Optional[float]] = mapped_column(Float)   # J2000
    dec_deg: Mapped[Optional[float]] = mapped_column(Float)
    magnitude: Mapped[Optional[float]] = mapped_column(Float)
    aliases: Mapped[Optional[dict]] = mapped_column(JSON)    # ["NGC1234","M42",...]
    extras: Mapped[Optional[dict]] = mapped_column(JSON)     # MPC ephemeris ref, AAVSO seq, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    knowledge_threads: Mapped[list["KnowledgeThread"]] = relationship(
        back_populates="target", cascade="all, delete-orphan",
    )


class KnowledgeThread(Base):
    """A research thread for a target (imaging / transit / astrometry /
    spectroscopy). Replaces a simple 'completion' status with a richer model.
    """
    __tablename__ = "knowledge_threads"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))           # "imaging", "transit", etc.
    state: Mapped[str] = mapped_column(String(16), default="dormant")  # dormant|active|mature|future
    open_question: Mapped[Optional[str]] = mapped_column(Text)
    threshold_to_unlock_next: Mapped[Optional[str]] = mapped_column(Text)
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                    onupdate=datetime.utcnow)

    target: Mapped["Target"] = relationship(back_populates="knowledge_threads")


class Campaign(Base):
    """A multi-night research effort with a goal, target(s), and success criterion."""
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    workflow: Mapped[WorkflowKind] = mapped_column(String(32))
    status: Mapped[CampaignStatus] = mapped_column(String(16), default=CampaignStatus.PROPOSED)
    priority: Mapped[int] = mapped_column(Integer, default=50)  # 1-100
    cadence: Mapped[Optional[str]] = mapped_column(String(64))  # "every_clear_night", "weekly", custom
    success_criterion: Mapped[Optional[dict]] = mapped_column(JSON)
    progress: Mapped[Optional[dict]] = mapped_column(JSON)
    deadline_utc: Mapped[Optional[datetime]] = mapped_column(DateTime)
    scientific_context: Mapped[Optional[str]] = mapped_column(Text)
    proposed_by: Mapped[Optional[str]] = mapped_column(String(32))  # "operator" | "oracle" | "external"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)

    targets: Mapped[list["CampaignTarget"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan",
    )


class CampaignTarget(Base):
    """Association: a campaign may track one or many targets."""
    __tablename__ = "campaign_targets"
    __table_args__ = (UniqueConstraint("campaign_id", "target_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    parameters: Mapped[Optional[dict]] = mapped_column(JSON)
    # workflow-specific: filter list, exposure plan, transit window, etc.

    campaign: Mapped["Campaign"] = relationship(back_populates="targets")


# ============================================================================
# Operations
# ============================================================================

class Session(Base):
    """One observing session — typically dusk to dawn of one night."""
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    state: Mapped[SessionState] = mapped_column(String(24), default=SessionState.PRE_SESSION)
    state_reason: Mapped[Optional[str]] = mapped_column(Text)
    simulation: Mapped[bool] = mapped_column(Boolean, default=False)
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    plan_blob: Mapped[Optional[dict]] = mapped_column(JSON)
    weather_summary: Mapped[Optional[dict]] = mapped_column(JSON)
    final_summary: Mapped[Optional[dict]] = mapped_column(JSON)


class Alert(Base):
    """Critic-raised alerts. Operator decides what to do with them."""
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sessions.id"), index=True)
    raised_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    severity: Mapped[AlertSeverity] = mapped_column(String(16))
    code: Mapped[str] = mapped_column(String(64))           # "dew_risk", "guiding_lost", etc.
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[Optional[dict]] = mapped_column(JSON)
    raised_by: Mapped[AgentName] = mapped_column(String(16))
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    resolution: Mapped[Optional[str]] = mapped_column(Text)


class Decision(Base):
    """Audit log of every major agent decision."""
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sessions.id"), index=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    agent: Mapped[AgentName] = mapped_column(String(16))
    decision_type: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[Optional[dict]] = mapped_column(JSON)
    outputs: Mapped[Optional[dict]] = mapped_column(JSON)
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    outcome: Mapped[Optional[str]] = mapped_column(Text)    # filled in retrospectively
    hindsight_verdict: Mapped[Optional[str]] = mapped_column(String(32))


class AgentMessage(Base):
    """Inter-agent message log (persistent audit trail)."""
    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    sender: Mapped[AgentName] = mapped_column(String(16))
    recipient: Mapped[AgentName] = mapped_column(String(16))
    kind: Mapped[AgentMessageKind] = mapped_column(String(32))
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sessions.id"), index=True)


# ============================================================================
# Data
# ============================================================================

class Frame(Base):
    """One captured frame (light, dark, bias, or flat)."""
    __tablename__ = "frames"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sessions.id"), index=True)
    target_id: Mapped[Optional[int]] = mapped_column(ForeignKey("targets.id"))
    captured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    frame_type: Mapped[str] = mapped_column(String(16))     # light|dark|bias|flat
    filter_name: Mapped[Optional[str]] = mapped_column(String(16))
    exposure_s: Mapped[float] = mapped_column(Float)
    gain: Mapped[Optional[int]] = mapped_column(Integer)
    offset: Mapped[Optional[int]] = mapped_column(Integer)
    ccd_temp_c: Mapped[Optional[float]] = mapped_column(Float)
    fwhm_arcsec: Mapped[Optional[float]] = mapped_column(Float)
    quality: Mapped[FrameQuality] = mapped_column(String(16), default=FrameQuality.UNGRADED)
    plate_solved: Mapped[bool] = mapped_column(Boolean, default=False)
    wcs_blob: Mapped[Optional[dict]] = mapped_column(JSON)
    fits_header: Mapped[Optional[dict]] = mapped_column(JSON)


class CalibrationMaster(Base):
    """A master bias, dark, or flat. Tracked for freshness."""
    __tablename__ = "calibration_masters"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))           # bias | dark | flat
    filter_name: Mapped[Optional[str]] = mapped_column(String(16))
    exposure_s: Mapped[Optional[float]] = mapped_column(Float)
    ccd_temp_c: Mapped[Optional[float]] = mapped_column(Float)
    gain: Mapped[Optional[int]] = mapped_column(Integer)
    offset: Mapped[Optional[int]] = mapped_column(Integer)
    file_path: Mapped[str] = mapped_column(String(512))
    n_frames: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ReferenceFrame(Base):
    """A reference image of a sky field, used for transient subtraction."""
    __tablename__ = "reference_frames"

    id: Mapped[int] = mapped_column(primary_key=True)
    field_key: Mapped[str] = mapped_column(String(64), index=True)  # e.g. "RA_DEC_HASH"
    target_id: Mapped[Optional[int]] = mapped_column(ForeignKey("targets.id"))
    filter_name: Mapped[Optional[str]] = mapped_column(String(16))
    file_path: Mapped[str] = mapped_column(String(512))
    n_visits_used: Mapped[int] = mapped_column(Integer, default=1)
    pixel_scale_arcsec: Mapped[float] = mapped_column(Float)
    depth_mag: Mapped[Optional[float]] = mapped_column(Float)
    fwhm_arcsec: Mapped[Optional[float]] = mapped_column(Float)
    wcs_blob: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)


class StackProduct(Base):
    """A finished stack — deep-sky integration, planetary stack, etc."""
    __tablename__ = "stack_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sessions.id"))
    target_id: Mapped[Optional[int]] = mapped_column(ForeignKey("targets.id"))
    workflow: Mapped[WorkflowKind] = mapped_column(String(32))
    file_path: Mapped[str] = mapped_column(String(512))
    thumbnail: Mapped[Optional[bytes]] = mapped_column(LargeBinary)  # ~50 KB thumb
    integration_s: Mapped[float] = mapped_column(Float, default=0.0)
    n_frames: Mapped[int] = mapped_column(Integer, default=0)
    rejection_stats: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================================================
# Science (measurement + submission)
# ============================================================================

class Measurement(Base):
    """A scientific measurement — astrometric position, photometric value,
    transient candidate. The unit of submission to MPC/AAVSO/TNS.
    """
    __tablename__ = "measurements"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sessions.id"))
    target_id: Mapped[Optional[int]] = mapped_column(ForeignKey("targets.id"))
    frame_id: Mapped[Optional[int]] = mapped_column(ForeignKey("frames.id"))
    workflow: Mapped[WorkflowKind] = mapped_column(String(32))
    kind: Mapped[MeasurementKind] = mapped_column(String(32))
    epoch_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    value: Mapped[Optional[dict]] = mapped_column(JSON)
    # astrometry: { ra_deg, dec_deg, ra_err_arcsec, dec_err_arcsec, ... }
    # photometry: { mag, mag_err, filter, comp_stars, ... }
    # transient:  { ra_deg, dec_deg, mag, snr, ref_diff, ... }
    quality: Mapped[Optional[str]] = mapped_column(String(16))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Submission(Base):
    """One pending or completed submission to an external service.

    Lifecycle: QUEUED -> APPROVED -> SUBMITTED -> ACK (or REJECTED / FAILED).
    """
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    measurement_id: Mapped[int] = mapped_column(ForeignKey("measurements.id"))
    destination: Mapped[SubmissionDestination] = mapped_column(String(32))
    status: Mapped[SubmissionStatus] = mapped_column(String(16),
                                                      default=SubmissionStatus.QUEUED,
                                                      index=True)
    formatted_payload: Mapped[Optional[str]] = mapped_column(Text)
    response_payload: Mapped[Optional[str]] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    rejected_reason: Mapped[Optional[str]] = mapped_column(Text)
    operator_notes: Mapped[Optional[str]] = mapped_column(Text)


# ============================================================================
# Storage events
# ============================================================================

class StorageEvent(Base):
    """Disk usage snapshots and cleanup events."""
    __tablename__ = "storage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    bytes_total: Mapped[int] = mapped_column(Integer)
    bytes_free: Mapped[int] = mapped_column(Integer)
    bytes_used_by_atlas: Mapped[Optional[int]] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32), default="snapshot")  # snapshot|cleanup|alert
    detail: Mapped[Optional[dict]] = mapped_column(JSON)


# Helpful composite indices
Index("ix_measurements_workflow_kind", Measurement.workflow, Measurement.kind)
Index("ix_submissions_status_dest", Submission.status, Submission.destination)
Index("ix_alerts_session_severity", Alert.session_id, Alert.severity)
