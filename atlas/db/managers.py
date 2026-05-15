"""Domain manager classes — thin layer above the ORM.

Each manager owns CRUD + business logic for one slice of the schema.
Agents and API routes use these instead of the ORM directly, so we can
swap implementations (e.g., for testing) and keep query logic in one place.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import desc, select

from atlas.db.models import (
    AgentMessage, AgentMessageKind, AgentName, Alert, AlertSeverity,
    Campaign, CampaignStatus, CampaignTarget, CalibrationMaster,
    Credential, Decision, EquipmentProfile, Frame, FrameQuality,
    KnowledgeThread, Measurement, MeasurementKind, NotificationConfig,
    ReferenceFrame, RetentionPolicy, Session as SessionRow, SessionState,
    SiteConfig, StackProduct, StorageEvent, Submission,
    SubmissionDestination, SubmissionStatus, Target, WorkflowKind,
)
from atlas.db.session import get_session
from atlas.logging_setup import get_logger

log = get_logger("db.managers")


# ---- Config -----------------------------------------------------------------

class ConfigManager:
    """Site config + equipment profile + retention + notifications."""

    @staticmethod
    def get_site() -> Optional[SiteConfig]:
        with get_session() as s:
            obj = s.query(SiteConfig).first()
            if obj:
                s.expunge(obj)
            return obj

    @staticmethod
    def save_site(**fields) -> SiteConfig:
        with get_session() as s:
            obj = s.query(SiteConfig).first()
            if obj is None:
                obj = SiteConfig(**fields)
                s.add(obj)
            else:
                for k, v in fields.items():
                    setattr(obj, k, v)
            s.flush()
            s.expunge(obj)
            return obj

    @staticmethod
    def get_equipment() -> Optional[EquipmentProfile]:
        with get_session() as s:
            obj = s.query(EquipmentProfile).first()
            if obj:
                s.expunge(obj)
            return obj

    @staticmethod
    def save_equipment(**fields) -> EquipmentProfile:
        with get_session() as s:
            obj = s.query(EquipmentProfile).first()
            if obj is None:
                obj = EquipmentProfile(**fields)
                s.add(obj)
            else:
                for k, v in fields.items():
                    setattr(obj, k, v)
            s.flush()
            s.expunge(obj)
            return obj

    @staticmethod
    def get_retention() -> RetentionPolicy:
        with get_session() as s:
            obj = s.query(RetentionPolicy).first()
            if obj is None:
                obj = RetentionPolicy()
                s.add(obj)
                s.flush()
            s.expunge(obj)
            return obj

    @staticmethod
    def get_notifications() -> NotificationConfig:
        with get_session() as s:
            obj = s.query(NotificationConfig).first()
            if obj is None:
                obj = NotificationConfig()
                s.add(obj)
                s.flush()
            s.expunge(obj)
            return obj


# ---- Credentials ------------------------------------------------------------

class CredentialManager:
    """Encrypted credentials accessor. Pairs with atlas.security.CredentialVault."""

    @staticmethod
    def set(key: str, plaintext: str, description: str | None = None) -> None:
        from atlas.security import get_vault
        vault = get_vault()
        blob = vault.encrypt(plaintext)
        with get_session() as s:
            obj = s.query(Credential).filter_by(key=key).first()
            if obj is None:
                obj = Credential(key=key, value_encrypted=blob,
                                 description=description)
                s.add(obj)
            else:
                obj.value_encrypted = blob
                if description is not None:
                    obj.description = description

    @staticmethod
    def get(key: str) -> Optional[str]:
        from atlas.security import get_vault
        vault = get_vault()
        with get_session() as s:
            obj = s.query(Credential).filter_by(key=key).first()
            if obj is None:
                return None
            return vault.decrypt(obj.value_encrypted)

    @staticmethod
    def has(key: str) -> bool:
        with get_session() as s:
            return s.query(Credential).filter_by(key=key).first() is not None

    @staticmethod
    def delete(key: str) -> None:
        with get_session() as s:
            obj = s.query(Credential).filter_by(key=key).first()
            if obj:
                s.delete(obj)


# ---- Sessions ---------------------------------------------------------------

class SessionManager:
    @staticmethod
    def start(simulation: bool = False) -> int:
        with get_session() as s:
            row = SessionRow(state=SessionState.PRE_SESSION, simulation=simulation)
            s.add(row)
            s.flush()
            return row.id

    @staticmethod
    def get(session_id: int) -> Optional[SessionRow]:
        with get_session() as s:
            obj = s.get(SessionRow, session_id)
            if obj:
                s.expunge(obj)
            return obj

    @staticmethod
    def latest() -> Optional[SessionRow]:
        with get_session() as s:
            obj = s.query(SessionRow).order_by(desc(SessionRow.started_at)).first()
            if obj:
                s.expunge(obj)
            return obj

    @staticmethod
    def set_state(session_id: int, state: SessionState, reason: str | None = None) -> None:
        with get_session() as s:
            obj = s.get(SessionRow, session_id)
            if obj:
                obj.state = state
                obj.state_reason = reason
                if state in (SessionState.SHUTDOWN, SessionState.COMPLETE):
                    obj.ended_at = datetime.utcnow()


# ---- Agent messages ---------------------------------------------------------

class AgentMessageManager:
    @staticmethod
    def log(sender: AgentName, recipient: AgentName, kind: AgentMessageKind,
            payload: dict | None = None, session_id: int | None = None) -> int:
        with get_session() as s:
            m = AgentMessage(sender=sender, recipient=recipient, kind=kind,
                              payload=payload, session_id=session_id)
            s.add(m)
            s.flush()
            return m.id

    @staticmethod
    def recent(limit: int = 100) -> list[AgentMessage]:
        with get_session() as s:
            rows = s.query(AgentMessage).order_by(desc(AgentMessage.sent_at)).limit(limit).all()
            for r in rows:
                s.expunge(r)
            return rows


# ---- Alerts -----------------------------------------------------------------

class AlertManager:
    @staticmethod
    def raise_alert(severity: AlertSeverity, code: str, message: str,
                    raised_by: AgentName, session_id: int | None = None,
                    data: dict | None = None) -> int:
        with get_session() as s:
            a = Alert(severity=severity, code=code, message=message,
                       raised_by=raised_by, session_id=session_id, data=data)
            s.add(a)
            s.flush()
            log.warning("ALERT[%s] %s — %s", severity.value, code, message)
            return a.id

    @staticmethod
    def acknowledge(alert_id: int, resolution: str | None = None) -> None:
        with get_session() as s:
            obj = s.get(Alert, alert_id)
            if obj:
                obj.acknowledged_at = datetime.utcnow()
                obj.resolution = resolution

    @staticmethod
    def unresolved(session_id: int | None = None) -> list[Alert]:
        with get_session() as s:
            q = s.query(Alert).filter(Alert.acknowledged_at.is_(None))
            if session_id is not None:
                q = q.filter(Alert.session_id == session_id)
            rows = q.order_by(desc(Alert.raised_at)).all()
            for r in rows:
                s.expunge(r)
            return rows


# ---- Decisions --------------------------------------------------------------

class DecisionManager:
    @staticmethod
    def log(agent: AgentName, decision_type: str, *,
            inputs: dict | None = None, outputs: dict | None = None,
            rationale: str | None = None, session_id: int | None = None) -> int:
        with get_session() as s:
            d = Decision(agent=agent, decision_type=decision_type, inputs=inputs,
                          outputs=outputs, rationale=rationale, session_id=session_id)
            s.add(d)
            s.flush()
            return d.id


# ---- Targets & campaigns ----------------------------------------------------

class TargetManager:
    @staticmethod
    def upsert(name: str, **fields) -> int:
        with get_session() as s:
            obj = s.query(Target).filter_by(name=name).first()
            if obj is None:
                obj = Target(name=name, **fields)
                s.add(obj)
            else:
                for k, v in fields.items():
                    setattr(obj, k, v)
            s.flush()
            return obj.id

    @staticmethod
    def find(name: str) -> Optional[Target]:
        with get_session() as s:
            obj = s.query(Target).filter_by(name=name).first()
            if obj:
                s.expunge(obj)
            return obj


class CampaignManager:
    @staticmethod
    def create(name: str, workflow: WorkflowKind, *, priority: int = 50,
               proposed_by: str = "operator", **fields) -> int:
        with get_session() as s:
            c = Campaign(name=name, workflow=workflow, priority=priority,
                          proposed_by=proposed_by, **fields)
            s.add(c)
            s.flush()
            return c.id

    @staticmethod
    def list_active() -> list[Campaign]:
        with get_session() as s:
            rows = s.query(Campaign).filter(
                Campaign.status == CampaignStatus.ACTIVE
            ).order_by(desc(Campaign.priority)).all()
            for r in rows:
                s.expunge(r)
            return rows

    @staticmethod
    def list_all() -> list[Campaign]:
        with get_session() as s:
            rows = s.query(Campaign).order_by(desc(Campaign.priority),
                                                desc(Campaign.created_at)).all()
            for r in rows:
                s.expunge(r)
            return rows

    @staticmethod
    def set_status(campaign_id: int, status: CampaignStatus) -> None:
        with get_session() as s:
            obj = s.get(Campaign, campaign_id)
            if obj:
                obj.status = status


# ---- Measurements & submissions --------------------------------------------

class MeasurementManager:
    @staticmethod
    def create(workflow: WorkflowKind, kind: MeasurementKind, epoch_utc: datetime,
               value: dict, *, target_id: int | None = None,
               frame_id: int | None = None, session_id: int | None = None,
               quality: str | None = None, notes: str | None = None) -> int:
        with get_session() as s:
            m = Measurement(
                workflow=workflow, kind=kind, epoch_utc=epoch_utc, value=value,
                target_id=target_id, frame_id=frame_id, session_id=session_id,
                quality=quality, notes=notes,
            )
            s.add(m)
            s.flush()
            return m.id


class SubmissionManager:
    """The human-approval submission queue."""

    @staticmethod
    def queue(measurement_id: int, destination: SubmissionDestination,
              formatted_payload: str | None = None) -> int:
        with get_session() as s:
            sub = Submission(
                measurement_id=measurement_id, destination=destination,
                status=SubmissionStatus.QUEUED, formatted_payload=formatted_payload,
            )
            s.add(sub)
            s.flush()
            return sub.id

    @staticmethod
    def list_queued() -> list[Submission]:
        with get_session() as s:
            rows = s.query(Submission).filter(
                Submission.status == SubmissionStatus.QUEUED
            ).order_by(Submission.queued_at).all()
            for r in rows:
                s.expunge(r)
            return rows

    @staticmethod
    def approve(submission_id: int, operator_notes: str | None = None) -> None:
        with get_session() as s:
            obj = s.get(Submission, submission_id)
            if obj and obj.status == SubmissionStatus.QUEUED:
                obj.status = SubmissionStatus.APPROVED
                obj.approved_at = datetime.utcnow()
                obj.operator_notes = operator_notes

    @staticmethod
    def reject(submission_id: int, reason: str) -> None:
        with get_session() as s:
            obj = s.get(Submission, submission_id)
            if obj and obj.status in (SubmissionStatus.QUEUED, SubmissionStatus.APPROVED):
                obj.status = SubmissionStatus.REJECTED
                obj.rejected_at = datetime.utcnow()
                obj.rejected_reason = reason

    @staticmethod
    def mark_submitted(submission_id: int, response: str | None = None) -> None:
        with get_session() as s:
            obj = s.get(Submission, submission_id)
            if obj:
                obj.status = SubmissionStatus.SUBMITTED
                obj.submitted_at = datetime.utcnow()
                if response is not None:
                    obj.response_payload = response


# ---- Calibration ------------------------------------------------------------

class CalibrationManager:
    @staticmethod
    def latest_master(kind: str, **filter_kwargs) -> Optional[CalibrationMaster]:
        with get_session() as s:
            q = s.query(CalibrationMaster).filter_by(kind=kind, **filter_kwargs)
            obj = q.order_by(desc(CalibrationMaster.created_at)).first()
            if obj:
                s.expunge(obj)
            return obj

    @staticmethod
    def is_stale(master: CalibrationMaster, freshness_days: int) -> bool:
        if master is None:
            return True
        return (datetime.utcnow() - master.created_at) > timedelta(days=freshness_days)


# ---- Storage events ---------------------------------------------------------

class StorageEventManager:
    @staticmethod
    def record(bytes_total: int, bytes_free: int,
               event_type: str = "snapshot",
               bytes_used_by_atlas: int | None = None,
               detail: dict | None = None) -> int:
        with get_session() as s:
            e = StorageEvent(
                bytes_total=bytes_total, bytes_free=bytes_free,
                bytes_used_by_atlas=bytes_used_by_atlas,
                event_type=event_type, detail=detail,
            )
            s.add(e)
            s.flush()
            return e.id

    @staticmethod
    def latest() -> Optional[StorageEvent]:
        with get_session() as s:
            obj = s.query(StorageEvent).order_by(desc(StorageEvent.recorded_at)).first()
            if obj:
                s.expunge(obj)
            return obj
