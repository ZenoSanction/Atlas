"""HTTP API routes.

Organised by dashboard tab. Each tab gets a route prefix:
    /api/health              system health
    /api/setup/*             setup wizard
    /api/tonight/*           live session + status
    /api/plan/*              campaigns + targets
    /api/science/*           submission queue
    /api/history/*           past sessions
    /api/atlas/*             chat
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from atlas import __version__
from atlas.agents.coordinator import get_coordinator
from atlas.agents.bus import Message, get_bus
from atlas.api.schemas import (
    CampaignCreate, ChatRequest, ChatResponse, EquipmentSchema,
    HealthResponse, InitVaultRequest, OperatorCommand, SetCredentialRequest,
    SetupStatus, SiteConfigSchema, SubmissionAction, UnlockVaultRequest,
)
from atlas.config import get_settings
from atlas.db.managers import (
    AlertManager, CampaignManager, ConfigManager, CredentialManager,
    SessionManager, SubmissionManager,
)
from atlas.db.models import (
    AgentMessageKind, AgentName, CampaignStatus, SubmissionStatus,
    WorkflowKind,
)
from atlas.logging_setup import get_logger
from atlas.security import get_vault
from atlas.storage.disk import DiskMonitor

log = get_logger("api")

api_router = APIRouter(prefix="/api")


# ============================================================================
# Health & root
# ============================================================================

@api_router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    s = get_settings()
    return HealthResponse(
        status="ok",
        version=__version__,
        simulation_mode=s.simulation_mode,
        agents=get_coordinator().status(),
    )


# ============================================================================
# Setup
# ============================================================================

@api_router.get("/setup/status", response_model=SetupStatus)
async def setup_status() -> SetupStatus:
    vault = get_vault()
    site = ConfigManager.get_site()
    equip = ConfigManager.get_equipment()
    return SetupStatus(
        vault_initialised=vault.is_initialised,
        site_configured=site is not None,
        equipment_configured=equip is not None,
        anthropic_key_set=CredentialManager.has("anthropic_api_key") if vault.is_unlocked else False,
        notifications_configured=CredentialManager.has("ntfy_topic") if vault.is_unlocked else False,
    )


@api_router.post("/setup/vault/init")
async def init_vault(req: InitVaultRequest) -> dict:
    vault = get_vault()
    if vault.is_initialised:
        raise HTTPException(409, "Vault already initialised")
    try:
        vault.initialise(req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@api_router.post("/setup/vault/unlock")
async def unlock_vault(req: UnlockVaultRequest) -> dict:
    vault = get_vault()
    if not vault.is_initialised:
        raise HTTPException(409, "Vault not initialised. Call /setup/vault/init first.")
    if not vault.unlock(req.password):
        raise HTTPException(401, "Incorrect master password")
    return {"ok": True}


@api_router.post("/setup/vault/lock")
async def lock_vault() -> dict:
    get_vault().lock()
    return {"ok": True}


@api_router.post("/setup/credentials")
async def set_credential(req: SetCredentialRequest) -> dict:
    if not get_vault().is_unlocked:
        raise HTTPException(401, "Vault is locked")
    CredentialManager.set(req.key, req.value, req.description)
    return {"ok": True}


@api_router.get("/setup/credentials/{key}/exists")
async def credential_exists(key: str) -> dict:
    return {"exists": CredentialManager.has(key)}


@api_router.put("/setup/site")
async def save_site(req: SiteConfigSchema) -> dict:
    ConfigManager.save_site(**req.model_dump())
    return {"ok": True}


@api_router.get("/setup/site")
async def get_site() -> Optional[dict]:
    s = ConfigManager.get_site()
    if s is None:
        return None
    return {c.name: getattr(s, c.name) for c in s.__table__.columns}


@api_router.put("/setup/equipment")
async def save_equipment(req: EquipmentSchema) -> dict:
    ConfigManager.save_equipment(**req.model_dump())
    return {"ok": True}


@api_router.get("/setup/equipment")
async def get_equipment() -> Optional[dict]:
    e = ConfigManager.get_equipment()
    if e is None:
        return None
    return {c.name: getattr(e, c.name) for c in e.__table__.columns}


# ============================================================================
# Tonight (live status)
# ============================================================================

@api_router.get("/tonight/status")
async def tonight_status() -> dict:
    sess = SessionManager.latest()
    agents = get_coordinator().status()
    disk = DiskMonitor().snapshot(record=False)
    alerts = [
        {"id": a.id, "severity": a.severity.value, "code": a.code,
          "message": a.message, "raised_at": a.raised_at.isoformat()}
        for a in AlertManager.unresolved()
    ]
    hardware = await _hardware_snapshot()
    return {
        "session": {
            "id": sess.id, "state": sess.state.value if hasattr(sess.state, "value") else sess.state,
            "started_at": sess.started_at.isoformat(),
            "simulation": sess.simulation,
        } if sess else None,
        "agents": agents,
        "hardware": hardware,
        "disk": {
            "gb_free": round(disk.gb_free, 2),
            "percent_used": round(disk.percent_used, 1),
            "bytes_used_by_atlas": disk.bytes_used_by_atlas,
        },
        "alerts": alerts,
    }


async def _hardware_snapshot() -> dict:
    """Best-effort snapshot of hardware status via NINA. Returns 'offline' on
    any failure so the dashboard can render without crashing if NINA is down.
    """
    out = {
        "camera":     {"connected": False, "status": "unknown"},
        "mount":      {"connected": False, "status": "unknown"},
        "focuser":    {"connected": False, "status": "unknown"},
        "filterwheel":{"connected": False, "status": "unknown"},
        "guiding":    {"connected": False, "status": "unknown"},
    }
    equip = ConfigManager.get_equipment()
    if equip is None:
        return out

    from atlas.config import get_settings
    if get_settings().simulation_mode:
        from atlas.simulation.fake_hardware import FakeNina, FakePhd2
        nina = FakeNina()
        phd2 = FakePhd2()
    else:
        from atlas.hardware.nina import NinaClient
        from atlas.hardware.phd2 import Phd2Client
        nina = NinaClient(host=equip.nina_host, port=equip.nina_port, timeout=2.0)
        phd2 = Phd2Client(host=equip.phd2_host, port=equip.phd2_port, timeout=2.0)

    try:
        try:
            info = await nina.camera_info()
            out["camera"] = {"connected": bool(info.get("connected")),
                              "temperature": info.get("temperature"),
                              "cooling": info.get("cooling"),
                              "status": "ok" if info.get("connected") else "disconnected"}
        except Exception as e:
            out["camera"]["status"] = f"error: {type(e).__name__}"

        try:
            info = await nina.focuser_info()
            out["focuser"] = {"connected": bool(info.get("connected")),
                               "position": info.get("position"),
                               "max_position": info.get("max_position"),
                               "status": "ok" if info.get("connected") else "disconnected"}
        except Exception as e:
            out["focuser"]["status"] = f"error: {type(e).__name__}"

        try:
            info = await nina.mount_info()
            out["mount"] = {"connected": bool(info.get("connected")),
                             "parked": info.get("parked"),
                             "tracking": info.get("tracking"),
                             "status": "ok" if info.get("connected") else "disconnected"}
        except Exception as e:
            out["mount"]["status"] = f"error: {type(e).__name__}"

        try:
            info = await nina.filterwheel_info()
            out["filterwheel"] = {"connected": bool(info.get("connected")),
                                    "current_filter": info.get("current_filter"),
                                    "status": "ok" if info.get("connected") else "disconnected"}
        except Exception:
            out["filterwheel"]["status"] = "n/a"

        try:
            state = await phd2.get_app_state()
            out["guiding"] = {"connected": True, "state": state, "status": "ok"}
        except Exception as e:
            out["guiding"]["status"] = f"error: {type(e).__name__}"
    finally:
        try:
            await nina.close()
        except Exception:
            pass
        try:
            await phd2.close()
        except Exception:
            pass
    return out


@api_router.post("/tonight/command")
async def operator_command(cmd: OperatorCommand) -> dict:
    """Human-issued operator command. Goes to the Operator agent's queue
    and overrides autonomous decisions."""
    sess = SessionManager.latest()
    session_id = sess.id if sess else None
    await get_bus().send(Message(
        sender=AgentName.OPERATOR,    # treated as human-via-operator
        recipient=AgentName.OPERATOR,
        kind=AgentMessageKind.OPERATOR_COMMAND,
        payload={"command": cmd.command, **cmd.params},
        session_id=session_id,
    ))
    return {"ok": True, "command": cmd.command}


# ============================================================================
# Plan (campaigns + targets)
# ============================================================================

@api_router.get("/plan/campaigns")
async def list_campaigns() -> list[dict]:
    rows = CampaignManager.list_all()
    return [
        {"id": r.id, "name": r.name, "workflow": r.workflow.value if hasattr(r.workflow, "value") else r.workflow,
          "status": r.status.value if hasattr(r.status, "value") else r.status,
          "priority": r.priority, "progress": r.progress or {},
          "scientific_context": r.scientific_context}
        for r in rows
    ]


@api_router.post("/plan/campaigns")
async def create_campaign(req: CampaignCreate) -> dict:
    try:
        wf = WorkflowKind(req.workflow)
    except ValueError:
        raise HTTPException(400, f"Unknown workflow kind: {req.workflow}")
    cid = CampaignManager.create(
        name=req.name, workflow=wf, priority=req.priority,
        cadence=req.cadence, scientific_context=req.scientific_context,
    )
    return {"ok": True, "id": cid}


@api_router.post("/plan/campaigns/{campaign_id}/activate")
async def activate_campaign(campaign_id: int) -> dict:
    CampaignManager.set_status(campaign_id, CampaignStatus.ACTIVE)
    return {"ok": True}


@api_router.post("/plan/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: int) -> dict:
    CampaignManager.set_status(campaign_id, CampaignStatus.PAUSED)
    return {"ok": True}


# ============================================================================
# Science (submission queue)
# ============================================================================

@api_router.get("/science/submissions")
async def list_submissions(status: str = "queued") -> list[dict]:
    """List pending submissions awaiting operator approval."""
    if status == "queued":
        rows = SubmissionManager.list_queued()
    else:
        # TODO Phase 2: filter by other statuses
        rows = SubmissionManager.list_queued()
    return [
        {"id": r.id, "destination": r.destination.value if hasattr(r.destination, "value") else r.destination,
          "status": r.status.value if hasattr(r.status, "value") else r.status,
          "measurement_id": r.measurement_id,
          "queued_at": r.queued_at.isoformat(),
          "formatted_payload": (r.formatted_payload or "")[:1024]}
        for r in rows
    ]


@api_router.post("/science/submissions/{submission_id}/action")
async def submission_action(submission_id: int, body: SubmissionAction) -> dict:
    if body.action == "approve":
        SubmissionManager.approve(submission_id, operator_notes=body.notes)
    elif body.action == "reject":
        SubmissionManager.reject(submission_id,
                                  reason=body.reason or "operator rejected")
    else:
        raise HTTPException(400, f"Unknown action: {body.action}")
    return {"ok": True}


# ============================================================================
# History
# ============================================================================

@api_router.get("/history/sessions")
async def list_sessions(limit: int = 50) -> list[dict]:
    # Minimal Phase 1: latest only. Phase 2 will paginate.
    s = SessionManager.latest()
    if s is None:
        return []
    return [{
        "id": s.id,
        "started_at": s.started_at.isoformat(),
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "state": s.state.value if hasattr(s.state, "value") else s.state,
        "simulation": s.simulation,
    }]


# ============================================================================
# ATLAS chat
# ============================================================================

@api_router.post("/atlas/chat", response_model=ChatResponse)
async def atlas_chat(req: ChatRequest) -> ChatResponse:
    """Talk to the Operator agent. Returns its text reply."""
    op = get_coordinator().get(AgentName.OPERATOR)
    if not CredentialManager.has("anthropic_api_key") if get_vault().is_unlocked else True:
        # Soft-fall to a canned reply if the key isn't configured
        if not get_vault().is_unlocked:
            return ChatResponse(
                reply="The credential vault is locked. Open Setup to unlock it.",
                safe_mode=True,
            )
    reply = await op.think(req.message)
    return ChatResponse(reply=reply, safe_mode=op.safe_mode)
