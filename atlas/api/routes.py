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

import asyncio
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
from atlas.config import get_settings, is_simulation_mode
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
    # Newly initialised vaults are also automatically unlocked.
    preflight = await _refresh_preflight_now(reason="vault_initialised")
    return {"ok": True, "preflight": preflight}


async def _refresh_preflight_now(reason: str = "vault state changed") -> dict:
    """Run the pre-flight gates immediately and publish the result.

    Normally the Operator agent runs this every 120 s on its own loop.
    That's fine for slow-moving conditions but feels broken when the
    operator just unlocked the vault and the dashboard's NO-GO banner
    keeps insisting "Blocked by: Credential vault" for up to two
    minutes. We poke the loop right after any state change that flips
    a gate so the UI feedback is instant."""
    from atlas.safety.preflight import run_session_preflight
    from atlas.agents.state import OperatorVerdict, get_state
    try:
        preflight = await run_session_preflight()
    except Exception as e:
        log.warning("Immediate preflight refresh failed (%s): %s", reason, e)
        return {"refreshed": False, "error": str(e)}
    pf_dict = preflight.to_jsonable()
    get_state().set_preflight(pf_dict)
    new_verdict = OperatorVerdict(
        decided_at=preflight.assessed_at,
        verdict=preflight.verdict,
        reason=preflight.reason,
        sources=["session_preflight", reason],
    )
    get_state().set_verdict(new_verdict)
    try:
        await get_bus().broadcast_event({
            "type": "session_preflight",
            "sender": "api",
            "kind": "preflight_verdict",
            "verdict": preflight.verdict,
            "reason": preflight.reason,
            "next_action": preflight.next_action,
            "triggered_by": reason,
            "sent_at": preflight.assessed_at,
        })
    except Exception:
        pass
    return {"refreshed": True, "verdict": preflight.verdict,
              "reason": preflight.reason}


@api_router.post("/setup/vault/unlock")
async def unlock_vault(req: UnlockVaultRequest) -> dict:
    vault = get_vault()
    if not vault.is_initialised:
        raise HTTPException(409, "Vault not initialised. Call /setup/vault/init first.")
    if not vault.unlock(req.password):
        raise HTTPException(401, "Incorrect master password")
    # Flip the pre-flight verdict immediately — otherwise the dashboard's
    # NO-GO banner stays stale for up to 2 minutes while the Operator's
    # background loop catches up.
    preflight = await _refresh_preflight_now(reason="vault_unlocked")
    return {"ok": True, "preflight": preflight}


@api_router.post("/setup/vault/lock")
async def lock_vault() -> dict:
    get_vault().lock()
    preflight = await _refresh_preflight_now(reason="vault_locked")
    return {"ok": True, "preflight": preflight}


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
        {"id": a.id,
          "severity": (a.severity.value if hasattr(a.severity, "value") else a.severity),
          "code": a.code,
          "message": a.message,
          "raised_at": a.raised_at.isoformat()}
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


# In-process cache for the hardware snapshot. The dashboard polls
# /api/tonight/status every 5 seconds; without this cache each poll
# made 5 serial NINA calls + 1 PHD2 call (~10 s total worst case),
# which saturated the browser's 6-concurrent-fetches-per-origin limit
# and starved the Weather / Plan / Science / History tabs of network
# slots. 10 s TTL gives the warm-room display a fresh-enough view
# while keeping NINA/PHD2 traffic bounded.
_HARDWARE_SNAPSHOT_CACHE: dict = {"at": 0.0, "data": None}
_HARDWARE_SNAPSHOT_TTL_S = 10.0
_HARDWARE_SNAPSHOT_HARD_TIMEOUT_S = 4.0


async def _hardware_snapshot() -> dict:
    """Best-effort snapshot of hardware status via NINA.

    Cached for ~10 s, wrapped in a 4 s hard timeout so a stalled NINA
    or PHD2 can't block the dashboard. Returns 'unknown' on timeout
    or any failure so the dashboard always renders.
    """
    import time as _time
    now = _time.monotonic()
    if (_HARDWARE_SNAPSHOT_CACHE["data"] is not None
            and (now - _HARDWARE_SNAPSHOT_CACHE["at"]) < _HARDWARE_SNAPSHOT_TTL_S):
        return _HARDWARE_SNAPSHOT_CACHE["data"]

    try:
        data = await asyncio.wait_for(_hardware_snapshot_inner(),
                                       timeout=_HARDWARE_SNAPSHOT_HARD_TIMEOUT_S)
    except asyncio.TimeoutError:
        data = {
            "camera":     {"connected": False, "status": "timeout"},
            "mount":      {"connected": False, "status": "timeout"},
            "focuser":    {"connected": False, "status": "timeout"},
            "filterwheel":{"connected": False, "status": "timeout"},
            "guiding":    {"connected": False, "status": "timeout"},
        }
        log.warning("Hardware snapshot exceeded %.1fs — returning timeout state",
                    _HARDWARE_SNAPSHOT_HARD_TIMEOUT_S)

    _HARDWARE_SNAPSHOT_CACHE["data"] = data
    _HARDWARE_SNAPSHOT_CACHE["at"] = now
    return data


async def _hardware_snapshot_inner() -> dict:
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

    if is_simulation_mode():
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
# Manual control (human override of autonomous Operator)
# ============================================================================

@api_router.get("/control/status")
async def control_status() -> dict:
    """Current take-control state + recent manual hardware actions.
    The dashboard polls this every few seconds to keep the MANUAL banner
    + Hardware Controls panel in sync."""
    from atlas.agents.state import get_state
    mc = get_state().get_manual_control()
    actions = get_state().get_manual_actions(limit=20)
    return {
        "engaged": mc.engaged,
        "engaged_at": mc.engaged_at,
        "released_at": mc.released_at,
        "reason": mc.reason,
        "engaged_by": mc.engaged_by,
        "action_count": mc.action_count,
        "last_action": mc.last_action,
        "actions": actions,
    }


@api_router.post("/control/take")
async def control_take(body: dict | None = None) -> dict:
    """Engage human override. Posts the take_control command onto the
    Operator's queue; the Operator's _cmd_take_control handler updates
    state and broadcasts the engagement event."""
    body = body or {}
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "reason required: explain why you're taking control")
    sess = SessionManager.latest()
    session_id = sess.id if sess else None
    await get_bus().send(Message(
        sender=AgentName.OPERATOR,
        recipient=AgentName.OPERATOR,
        kind=AgentMessageKind.OPERATOR_COMMAND,
        payload={"command": "take_control",
                  "params": {"reason": reason,
                              "by": body.get("by") or "operator"}},
        session_id=session_id,
    ))
    return {"ok": True, "reason": reason}


@api_router.post("/control/release")
async def control_release(body: dict | None = None) -> dict:
    """Release human override. Operator resumes autonomy on next cycle."""
    body = body or {}
    reason = (body.get("reason") or "operator released control").strip()
    sess = SessionManager.latest()
    session_id = sess.id if sess else None
    await get_bus().send(Message(
        sender=AgentName.OPERATOR,
        recipient=AgentName.OPERATOR,
        kind=AgentMessageKind.OPERATOR_COMMAND,
        payload={"command": "release_control",
                  "params": {"reason": reason}},
        session_id=session_id,
    ))
    return {"ok": True, "reason": reason}


_VALID_MANUAL_KINDS = {
    "slew", "park", "unpark", "capture", "set_cooling", "warmup",
    "move_focuser", "change_filter", "dome_open", "dome_close",
}


@api_router.post("/control/command")
async def control_command(body: dict | None = None) -> dict:
    """Issue a single direct hardware command. Only honoured while manual
    control is engaged — the Operator's _cmd_manual_action handler enforces
    that and records every attempt in the audit ring buffer.

    Body shape:
        { "kind": "slew", "args": {"ra_hours": 5.6, "dec_deg": -5.4},
          "rationale": "Recentre on Orion Nebula for visual check" }
    """
    from atlas.agents.state import get_state
    body = body or {}
    kind = (body.get("kind") or "").lower().strip()
    args = body.get("args") or {}
    rationale = (body.get("rationale") or "").strip()
    if kind not in _VALID_MANUAL_KINDS:
        raise HTTPException(400, f"unknown kind '{kind}'. Valid: {sorted(_VALID_MANUAL_KINDS)}")
    if not rationale:
        raise HTTPException(400, "rationale required for every manual action (audit trail)")
    if not get_state().is_manual():
        raise HTTPException(409,
            "Manual control is not engaged. POST /api/control/take first.")
    sess = SessionManager.latest()
    session_id = sess.id if sess else None
    await get_bus().send(Message(
        sender=AgentName.OPERATOR,
        recipient=AgentName.OPERATOR,
        kind=AgentMessageKind.OPERATOR_COMMAND,
        payload={"command": "manual_action",
                  "params": {"kind": kind, "args": args,
                              "rationale": rationale}},
        session_id=session_id,
    ))
    return {"ok": True, "kind": kind, "rationale": rationale}


# ============================================================================
# Plan (campaigns + targets)
# ============================================================================

@api_router.get("/plan/campaigns")
async def list_campaigns() -> list[dict]:
    from atlas.db.models import CampaignTarget
    from atlas.db.session import get_session
    from sqlalchemy import func
    rows = CampaignManager.list_all()
    # Pull target counts per campaign in one query so the dashboard
    # can show "M51 deep — 3 targets" without N+1 round-trips.
    counts: dict[int, int] = {}
    try:
        with get_session() as s:
            for cid, n in (s.query(CampaignTarget.campaign_id,
                                       func.count(CampaignTarget.id))
                              .group_by(CampaignTarget.campaign_id).all()):
                counts[cid] = int(n)
    except Exception:
        pass
    return [
        {"id": r.id, "name": r.name,
          "workflow": r.workflow.value if hasattr(r.workflow, "value") else r.workflow,
          "status": r.status.value if hasattr(r.status, "value") else r.status,
          "priority": r.priority, "progress": r.progress or {},
          "scientific_context": r.scientific_context,
          "target_count": counts.get(r.id, 0)}
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


# ----------------------------------------------------------------------------
# Target search + add-to-campaign  (dashboard's per-campaign modal)
# ----------------------------------------------------------------------------

@api_router.get("/plan/targets/search")
async def search_targets(q: str = "", fallback_simbad: bool = False,
                          limit: int = 15) -> dict:
    """Find targets by name. Searches the local seasonal catalog first.
    If `fallback_simbad=true` and the catalog yields no hits, also
    queries SIMBAD's TAP service for the long tail (NGC/IC/Sh2/Bayer
    designations, comets by name, etc.).

    Each match is enriched with *tonight's* visibility: the peak
    altitude reached during the astronomical dark window plus the
    rise/set times within it. That's far more useful than "is it up
    right now?" for a search modal — the operator is planning what to
    image *tonight*, not snapping pictures right now. Results from
    SIMBAD are tagged `source: "simbad"`.
    """
    from atlas.astronomy.catalog import search as catalog_search
    from atlas.astronomy.visibility import night_window
    from atlas.astronomy.scheduler import compute_visibility_window
    from datetime import datetime as _dt
    site = ConfigManager.get_site()
    lat = float(site.latitude) if site else 0.0
    lon = float(site.longitude) if site else 0.0
    horizon = float(site.horizon_alt_min) if site else 20.0
    now = _dt.utcnow()

    # Compute tonight's astronomical dark window once — every result
    # uses the same window so the visibility numbers are comparable.
    nw = None
    if site is not None:
        nw = night_window(lat, lon, now, altitude_deg=-18.0)

    def enrich(entry: dict, source: str) -> dict:
        ra, dec = entry.get("ra_deg"), entry.get("dec_deg")
        out = dict(entry); out["source"] = source
        if ra is None or dec is None or site is None or nw is None:
            out["visible_tonight"] = None
            out["peak_alt_deg"] = None
            return out
        dusk, dawn = nw
        try:
            vis = compute_visibility_window(
                {"ra_deg": float(ra), "dec_deg": float(dec)},
                lat=lat, lon=lon, horizon_alt=horizon,
                dusk=dusk, dawn=dawn,
            )
        except Exception:
            vis = None
        if vis is None:
            out["visible_tonight"] = False
            out["peak_alt_deg"] = None
        else:
            out["visible_tonight"] = True
            out["peak_alt_deg"] = vis.peak_alt_deg
            out["visible_from_utc"] = (
                vis.visible_from.isoformat(timespec="seconds") + "Z")
            out["visible_until_utc"] = (
                vis.visible_until.isoformat(timespec="seconds") + "Z")
            out["visible_minutes"] = round(vis.length_minutes, 0)
        return out

    catalog_hits = [enrich(e, "catalog") for e in catalog_search(q, limit=limit)]
    if catalog_hits or not fallback_simbad or not q.strip():
        return {"query": q, "results": catalog_hits,
                  "site_configured": site is not None}

    # Catalog miss → SIMBAD
    from atlas.astronomy.simbad import resolve as simbad_resolve
    sim = await simbad_resolve(q)
    if sim is None:
        return {"query": q, "results": [],
                  "site_configured": site is not None,
                  "simbad_tried": True, "simbad_found": False}
    sim_entry = {
        "name": sim.main_id, "alt_names": [q] if q != sim.main_id else [],
        "ra_deg": sim.ra_deg, "dec_deg": sim.dec_deg,
        "magnitude": sim.magnitude, "object_type": sim.object_type,
        "best_months": [], "notes": "(resolved by SIMBAD)",
    }
    return {"query": q, "results": [enrich(sim_entry, "simbad")],
              "site_configured": site is not None,
              "simbad_tried": True, "simbad_found": True}


@api_router.get("/plan/campaigns/{campaign_id}/targets")
async def list_campaign_targets(campaign_id: int) -> list[dict]:
    """Targets currently linked to a campaign — used by the modal to
    show "already added" so the operator can't double-link by accident."""
    from atlas.db.managers import TargetManager
    rows = TargetManager.list_for_campaign(campaign_id)
    return [{"id": t.id, "name": t.name, "object_type": t.object_type,
              "ra_deg": t.ra_deg, "dec_deg": t.dec_deg,
              "magnitude": t.magnitude} for t in rows]


@api_router.post("/plan/campaigns/{campaign_id}/targets")
async def add_target_to_campaign(campaign_id: int, body: dict) -> dict:
    """Upsert a Target row + link it to a campaign. Body is the search
    result dict (or any object with name + ra_deg + dec_deg). Idempotent:
    duplicate calls return `{"ok": true, "already_linked": true}`."""
    from atlas.db.managers import CampaignManager, TargetManager
    name = (body.get("name") or body.get("main_id") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    ra = body.get("ra_deg"); dec = body.get("dec_deg")
    if ra is None or dec is None:
        raise HTTPException(400, "ra_deg and dec_deg are required")
    # Confirm the campaign exists
    existing_campaigns = {c.id: c for c in CampaignManager.list_all()}
    if campaign_id not in existing_campaigns:
        raise HTTPException(404, f"Campaign {campaign_id} not found")

    aliases = body.get("alt_names") or body.get("aliases") or []
    target_id = TargetManager.upsert(
        name=name,
        ra_deg=float(ra), dec_deg=float(dec),
        object_type=body.get("object_type") or "unknown",
        magnitude=(float(body["magnitude"])
                     if body.get("magnitude") is not None else None),
        aliases=list(aliases) if aliases else None,
    )
    created = TargetManager.link_to_campaign(campaign_id, target_id)
    return {"ok": True, "target_id": target_id,
              "campaign_id": campaign_id,
              "linked_now": created, "already_linked": not created}


@api_router.delete("/plan/campaigns/{campaign_id}/targets/{target_id}")
async def remove_target_from_campaign(campaign_id: int,
                                          target_id: int) -> dict:
    from atlas.db.managers import TargetManager
    removed = TargetManager.unlink_from_campaign(campaign_id, target_id)
    return {"ok": True, "removed": removed}


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


# ============================================================================
# Weather + GO/NO-GO verdict
# ============================================================================

@api_router.get("/weather/current")
async def weather_current(force_refresh: bool = False) -> dict:
    """Current-conditions snapshot at the configured site.

    Reads through the process-wide WeatherCache — same single
    underlying snapshot every agent sees. Set ``force_refresh=true``
    in the query string to bypass the TTL (rare; the cache refreshes
    itself when stale)."""
    site = ConfigManager.get_site()
    if site is None:
        raise HTTPException(409, "Site coordinates not configured. Open Setup.")
    from atlas.weather.cache import get_weather_cache
    try:
        state = await get_weather_cache().get(
            lat=float(site.latitude), lon=float(site.longitude),
            force_refresh=force_refresh,
        )
    except Exception as e:
        raise HTTPException(502, f"Weather cache failure: {e}")
    snap = state.snapshot
    if snap is None:
        raise HTTPException(503, "No weather data available yet — cache is "
                                  "empty and refresh failed.")
    from atlas.units import (
        c_to_f, c_delta_to_f, ms_to_mph, mm_to_in, hpa_to_inhg,
    )
    return {
        "observed_at": snap.observed_at,
        "cache_age_seconds": state.age_seconds,
        "cache_fresh": state.fresh,
        "cache_refreshed_this_call": state.refreshed_this_call,
        "temperature_f": round(c_to_f(snap.temperature_c), 1),
        "humidity_pct": round(snap.humidity_pct, 0),
        "dew_point_f": round(c_to_f(snap.dew_point_c), 1),
        "dew_margin_f": round(c_delta_to_f(snap.temperature_c - snap.dew_point_c), 1),
        "wind_speed_mph": round(ms_to_mph(snap.wind_speed_ms), 1),
        "wind_gust_mph": (round(ms_to_mph(snap.wind_gust_ms), 1)
                            if snap.wind_gust_ms is not None else None),
        "cloud_cover_pct": round(snap.cloud_cover_pct, 0),
        "pressure_inhg": round(hpa_to_inhg(snap.pressure_hpa), 2),
        "precip_in": round(mm_to_in(snap.precip_mm), 3),
        "site_lat": float(site.latitude),
        "site_lon": float(site.longitude),
        "observatory_name": site.observatory_name,
    }


@api_router.get("/weather/forecast")
async def weather_forecast(hours: int = 48, nighttime_only: bool = True) -> dict:
    """Hourly forecast from Open-Meteo.

    With ``nighttime_only=True`` (the default), returns *only the coming
    night's* astronomical-dark hours (sun < -18°). Specifically:
      - if we're currently inside astronomical dark, returns the
        remaining hours up to dawn
      - if we're in daytime / twilight, returns the upcoming
        dusk → dawn window

    No hours from tomorrow night or beyond — exactly one night.

    Set ``nighttime_only=False`` to get every hour back, daytime
    included (rarely useful — sky safety alerts already cover the
    rare "is a storm rolling in at noon" case)."""
    hours = max(1, min(48, int(hours)))
    site = ConfigManager.get_site()
    if site is None:
        raise HTTPException(409, "Site coordinates not configured. Open Setup.")
    # Read forecast through the cache. Cache keeps a 12-hour forecast
    # by default; if the caller asks for more we fall back to a direct
    # client call (rare).
    from atlas.weather.cache import get_weather_cache
    try:
        state = await get_weather_cache().get(
            lat=float(site.latitude), lon=float(site.longitude),
            forecast_hours=max(12, hours),
        )
        rows = list(state.forecast_hours or [])[:hours]
    except Exception as e:
        raise HTTPException(502, f"Weather cache failure: {e}")
    if not rows:
        # Cache might be empty on cold boot before first refresh — try
        # a direct one-off pull so the page isn't blank on first visit.
        from atlas.weather.openmeteo import OpenMeteoClient
        try:
            client = OpenMeteoClient(latitude=float(site.latitude),
                                       longitude=float(site.longitude))
            rows = await client.forecast_hours(hours=hours)
        except Exception as e:
            raise HTTPException(502, f"Open-Meteo request failed: {e}")

    # Astronomical dark window filter: pin to the *coming night* exactly.
    night_meta = None
    if nighttime_only:
        from atlas.astronomy import sun_altitude, night_window
        from datetime import timedelta as _td
        lat = float(site.latitude); lon = float(site.longitude)
        now = datetime.utcnow()

        # Compute the relevant dusk/dawn pair. If we're already in dark,
        # find the dawn coming up next; otherwise find the next dusk
        # then the dawn after it.
        currently_dark = sun_altitude(lat, lon, now) < -18.0
        if currently_dark:
            # We're mid-night. Find dawn (next sun crossing of -18°
            # ascending). Use night_window from 12h ago so the search
            # still brackets the current dusk that already happened.
            nw = night_window(lat, lon, now - _td(hours=12), altitude_deg=-18.0)
        else:
            nw = night_window(lat, lon, now, altitude_deg=-18.0)

        if nw is not None:
            dusk, dawn = nw
            night_meta = {
                "dusk_utc": dusk.isoformat(timespec="seconds") + "Z",
                "dawn_utc": dawn.isoformat(timespec="seconds") + "Z",
                "hours": round((dawn - dusk).total_seconds() / 3600, 2),
                "twilight": "astronomical_-18",
                "in_progress": currently_dark,
            }
            # Keep ONLY rows that fall inside [max(now, dusk), dawn].
            # That's "the rest of this night" if we're already dark,
            # or "the upcoming night" otherwise.
            window_start = max(now, dusk)
            kept: list[dict] = []
            for r in rows:
                try:
                    t = datetime.fromisoformat(r["time"])
                except Exception:
                    continue
                if window_start <= t < dawn:
                    kept.append(r)
            rows = kept
        else:
            # Polar day or no dark window in 36h — return empty list,
            # the dashboard handles this case gracefully.
            rows = []
    from atlas.units import c_to_f, c_delta_to_f, ms_to_mph, mm_to_in
    out_rows = []
    for r in rows:
        dm_c = r["temperature_c"] - r["dew_point_c"]
        # Open-Meteo returns UTC times without the trailing 'Z', so the
        # browser parses them as local time and the displayed timestamps
        # come out 4-5h off (depending on EDT/EST). Tag explicitly.
        time_raw = r["time"]
        time_utc = time_raw if time_raw.endswith("Z") else time_raw + "Z"
        out_rows.append({
            "time_utc": time_utc,
            "temperature_f": round(c_to_f(r["temperature_c"]), 1),
            "humidity_pct": round(r["humidity_pct"], 0),
            "dew_point_f": round(c_to_f(r["dew_point_c"]), 1),
            "dew_margin_f": round(c_delta_to_f(dm_c), 1),
            "wind_speed_mph": round(ms_to_mph(r["wind_speed_ms"]), 1),
            "wind_gust_mph": (round(ms_to_mph(r["wind_gust_ms"]), 1)
                                if r.get("wind_gust_ms") is not None else None),
            "cloud_cover_pct": round(r["cloud_cover_pct"], 0),
            "precip_in": round(mm_to_in(r["precip_mm"]), 3),
        })
    return {
        "hours": hours,
        "nighttime_only": nighttime_only,
        "night": night_meta,
        "site_lat": float(site.latitude),
        "site_lon": float(site.longitude),
        "observatory_name": site.observatory_name,
        "hourly": out_rows,
    }


@api_router.get("/critic/assessment")
async def critic_assessment() -> dict:
    """The Critic's latest weather assessment (per-metric pass/fail).
    Returns null if the Critic hasn't run yet (just started, no site config, etc.)."""
    from atlas.agents.state import get_state
    a = get_state().get_assessment()
    if a is None:
        return {"assessment": None}
    return {"assessment": a.to_jsonable()}


@api_router.get("/operator/verdict")
async def operator_verdict() -> dict:
    """The Operator's latest GO / CAUTION / NO-GO decision.
    Returns UNKNOWN until the Critic has reported in."""
    from atlas.agents.state import get_state
    v = get_state().get_verdict()
    if v is None:
        return {"verdict": "UNKNOWN", "reason": "Awaiting first Critic assessment.",
                 "decided_at": None, "sources": []}
    return v.to_jsonable()


@api_router.get("/preflight")
async def session_preflight() -> dict:
    """Latest comprehensive session-readiness pre-flight.

    Aggregates 8 gates (weather, hardware, calibration, plan, disk,
    vault, API health, dark window) into a single verdict
    (GO / WAITING / CAUTION / NO-GO / UNKNOWN). Run every 2 minutes by
    the Operator agent; the dashboard's Session Readiness panel polls
    this endpoint."""
    from atlas.agents.state import get_state
    pf = get_state().get_preflight()
    if pf is None:
        return {"verdict": "UNKNOWN",
                 "reason": "Operator pre-flight hasn't run yet.",
                 "next_action": "Wait ~2 minutes for the first cycle.",
                 "assessed_at": None,
                 "gates": []}
    return pf


# ============================================================================
# Weather thresholds (Setup tab)
# ============================================================================

@api_router.get("/setup/notifications/channels")
async def notification_channels() -> dict:
    """Per-channel configured status (ntfy / email / webhook /
    whatever's registered). Setup-tab renders this so the operator
    sees which channels are ready to fire vs which need
    credentials."""
    from atlas.notifications import get_dispatcher
    return {"channels": get_dispatcher().channel_status()}


@api_router.post("/setup/notifications/test")
async def notification_test() -> dict:
    """Fire a single test notification through every configured
    channel. Returns per-channel pass/fail so the operator can
    verify their routing before relying on it overnight."""
    from atlas.notifications import Notification, get_dispatcher
    n = Notification(
        severity="critical",
        title="ATLAS test notification",
        message="If you can read this, alerts are working.",
        detail="Triggered manually from Setup → Notifications.",
        source="setup",
        tags=["test"],
    )
    results = await get_dispatcher().dispatch(n)
    return {"ok": True, "results": results,
              "channels_tried": len(results),
              "channels_succeeded": sum(1 for v in results.values() if v)}


@api_router.get("/setup/external-tools")
async def get_external_tools() -> dict:
    """Status of external binaries (ASTAP, Siril, etc.) checked at
    boot. Each entry: configured / available / detail. Dashboard
    surfaces these so the operator knows what's working before a
    session needs it."""
    from atlas.startup_checks import get_tool_status
    return {"tools": get_tool_status()}


@api_router.get("/setup/system-flags")
async def get_system_flags() -> dict:
    """Return runtime-mutable flags. simulation_mode here is the DB
    value; env-var ATLAS_SIMULATION_MODE still wins if set (effective
    flag is surfaced via /api/mission-control). auto_start_sessions
    gates the Operator's autonomous start path — see the Setup-tab
    description for the full preconditions."""
    flags = ConfigManager.get_system_flags()
    return {
        "simulation_mode_db": bool(flags.simulation_mode),
        "simulation_mode_effective": is_simulation_mode(),
        "auto_start_sessions": bool(
            getattr(flags, "auto_start_sessions", False)),
        "env_override_set": ((__import__("os").environ.get("ATLAS_SIMULATION_MODE", "")
                              ).lower() in ("1", "true", "yes", "on")),
        "updated_at": flags.updated_at.isoformat() if flags.updated_at else None,
    }


@api_router.post("/setup/seed-bench-campaign")
async def seed_bench_campaign_route() -> dict:
    """Idempotently create the Bench-test campaign and pre-link 4
    well-placed targets. Safe to call repeatedly — re-runs only add
    targets that are missing, never duplicate links."""
    from atlas.db.seed_bench import seed_bench_campaign
    result = seed_bench_campaign()
    # Nudge the Planner so the new campaign shows up immediately
    # instead of waiting for its 30-min rebuild cycle.
    try:
        await get_bus().send(Message(
            sender=AgentName.OPERATOR,
            recipient=AgentName.PLANNER,
            kind=AgentMessageKind.REVISION_REQUEST,
            payload={"reason": "bench-test campaign seeded"},
        ))
    except Exception:
        pass
    return result


@api_router.post("/setup/system-flags")
async def save_system_flags(body: dict) -> dict:
    """Patch runtime flags: simulation_mode + auto_start_sessions."""
    allowed = {"simulation_mode", "auto_start_sessions"}
    bad = set(body.keys()) - allowed
    if bad:
        raise HTTPException(400, f"Unknown fields: {sorted(bad)}")
    fields = {}
    if "simulation_mode" in body:
        fields["simulation_mode"] = bool(body["simulation_mode"])
    if "auto_start_sessions" in body:
        fields["auto_start_sessions"] = bool(body["auto_start_sessions"])
    ConfigManager.save_system_flags(**fields)
    # Refresh the verdict immediately so the dashboard doesn't lag.
    preflight = await _refresh_preflight_now(reason="system_flags_changed")
    return {"ok": True,
              "simulation_mode_effective": is_simulation_mode(),
              "auto_start_sessions":
                bool(ConfigManager.get_system_flags().auto_start_sessions
                       if hasattr(ConfigManager.get_system_flags(), "auto_start_sessions")
                       else False),
              "preflight": preflight}


@api_router.get("/setup/tls")
async def get_tls_info() -> dict:
    """Return info about the self-signed cert + whether the request that
    delivered THIS page came in over HTTPS. The Setup-tab panel reads
    this to tell the operator: 'yes the dashboard is reachable from your
    warm-room device, here's the fingerprint to verify, here's when it
    expires.'"""
    from atlas.tls import load_cert_info, discover_local_ips
    info = load_cert_info()
    return {
        "cert_present": info is not None,
        "cert": info.to_jsonable() if info else None,
        "local_ips": discover_local_ips(),
        "tls_dir": str((__import__("atlas.tls", fromlist=["tls_dir"])).tls_dir()),
        "hint": ("Restart ATLAS to pick up a regenerated cert. From a "
                  "warm-room device, visit https://<observatory-ip>:5000, "
                  "click Advanced → Proceed once, and you're set."),
    }


@api_router.post("/setup/tls/regenerate")
async def regenerate_tls(body: dict | None = None) -> dict:
    """Wipe + re-mint the self-signed cert. Operator clicks this in
    Setup when the observatory PC's IP has changed (e.g. moved to a new
    network) so the SAN list catches up. New cert takes effect on the
    next server restart."""
    from atlas.tls import generate_cert
    info = generate_cert(force=True)
    return {"ok": True, "cert": info.to_jsonable(),
              "note": "Restart ATLAS to start serving the new cert."}


@api_router.get("/setup/weather-thresholds")
async def get_weather_thresholds() -> dict:
    """Return thresholds in imperial display units. Internally stored in SI."""
    from atlas.units import ms_to_mph, c_delta_to_f
    t = ConfigManager.get_weather_thresholds()
    return {
        "wind_speed_warn_mph": round(ms_to_mph(t.wind_speed_warn_ms), 1),
        "wind_speed_critical_mph": round(ms_to_mph(t.wind_speed_critical_ms), 1),
        "humidity_warn_pct": t.humidity_warn_pct,
        "humidity_critical_pct": t.humidity_critical_pct,
        "dew_margin_warn_f": round(c_delta_to_f(t.dew_margin_warn_c), 1),
        "dew_margin_critical_f": round(c_delta_to_f(t.dew_margin_critical_c), 1),
        "cloud_cover_warn_pct": t.cloud_cover_warn_pct,
        "cloud_cover_critical_pct": t.cloud_cover_critical_pct,
    }


@api_router.post("/setup/weather-thresholds")
async def save_weather_thresholds(body: dict) -> dict:
    """Accept thresholds in imperial. Convert to SI for storage."""
    from atlas.units import mph_to_ms, f_delta_to_c
    allowed = {
        "wind_speed_warn_mph", "wind_speed_critical_mph",
        "humidity_warn_pct", "humidity_critical_pct",
        "dew_margin_warn_f", "dew_margin_critical_f",
        "cloud_cover_warn_pct", "cloud_cover_critical_pct",
    }
    bad = set(body.keys()) - allowed
    if bad:
        raise HTTPException(400, f"Unknown fields: {sorted(bad)}")
    si_fields = {}
    for k, v in body.items():
        v = float(v)
        if k == "wind_speed_warn_mph":
            si_fields["wind_speed_warn_ms"] = mph_to_ms(v)
        elif k == "wind_speed_critical_mph":
            si_fields["wind_speed_critical_ms"] = mph_to_ms(v)
        elif k == "dew_margin_warn_f":
            si_fields["dew_margin_warn_c"] = f_delta_to_c(v)
        elif k == "dew_margin_critical_f":
            si_fields["dew_margin_critical_c"] = f_delta_to_c(v)
        else:
            si_fields[k] = v
    ConfigManager.save_weather_thresholds(**si_fields)
    return {"ok": True}


# ============================================================================
# Plan — tonight's visible targets
# ============================================================================

@api_router.get("/plan/tonight")
async def plan_tonight() -> dict:
    """The Planner's latest visible-target list. Refreshed every 30 min and
    on REVISION_REQUEST from the Operator. Returns null when no plan exists
    yet (e.g., no active campaigns, no site config)."""
    from atlas.agents.state import get_state
    plan = get_state().get_tonight_plan()
    return {"plan": plan}


# ============================================================================
# Agent activity (post-session + research summaries)
# ============================================================================

@api_router.get("/agents/activity")
async def agents_activity() -> dict:
    """Latest stored activity from Archivist + Oracle, for the Tonight
    tab's Agent Activity card."""
    from atlas.agents.state import get_state
    st = get_state()
    return {
        "archivist": st.get_archivist_last(),
        "oracle": st.get_oracle_last(),
    }


# ============================================================================
# Mission Control — per-agent live state + chat
# ============================================================================

_AGENT_NAMES = {
    "planner": AgentName.PLANNER,
    "critic": AgentName.CRITIC,
    "operator": AgentName.OPERATOR,
    "archivist": AgentName.ARCHIVIST,
    "oracle": AgentName.ORACLE,
}


@api_router.get("/mission-control")
async def mission_control() -> dict:
    """Snapshot for the Mission Control dashboard view: per-agent live
    status, the latest verdict, and the recent message-flow buffer."""
    from atlas.agents.state import get_state
    from atlas.db.managers import MemoryManager
    st = get_state()
    coord_status = get_coordinator().status()
    settings = get_settings()
    site = ConfigManager.get_site()
    agents = {}
    for name, status in st.get_all_agent_status().items():
        d = status.to_jsonable()
        c = coord_status.get(name, {})
        d["running"] = c.get("running", False)
        d["safe_mode"] = c.get("safe_mode", False)
        d["memory_count"] = MemoryManager.count_for(name, include_shared=True)
        agents[name] = d
    verdict = st.get_verdict()
    mc = st.get_manual_control()
    # Weather cache state — the dashboard surfaces the current polling
    # mode (idle / active / borderline) so the operator can see how
    # tight the monitoring is right now. age_seconds tells them when
    # the cached snapshot was pulled.
    try:
        from atlas.weather.cache import get_weather_cache
        wc = get_weather_cache().peek()
        weather_monitor = {
            "mode": wc.mode, "ttl_s": wc.ttl_s,
            "age_seconds": wc.age_seconds, "fresh": wc.fresh,
            "snapshot_at": (wc.pulled_at.isoformat(timespec="seconds") + "Z"
                              if wc.pulled_at else None),
        }
    except Exception:
        weather_monitor = None
    return {
        "now_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "simulation_mode": is_simulation_mode(),
        "observatory_name": (site.observatory_name if site else None),
        "verdict": verdict.to_jsonable() if verdict else None,
        "preflight": st.get_preflight(),
        "session_review": st.get_session_review(),
        "manual_control": mc.to_jsonable(),
        "weather_monitor": weather_monitor,
        "agents": agents,
        "message_flow": st.get_message_flow(limit=40),
    }


@api_router.get("/agents/{agent_name}/state")
async def agent_state(agent_name: str) -> dict:
    """Live state for one agent — what it's doing, recent decisions, etc."""
    if agent_name not in _AGENT_NAMES:
        raise HTTPException(404, f"Unknown agent: {agent_name}")
    from atlas.agents.state import get_state
    st = get_state()
    status = st.get_agent_status(agent_name)
    coord_status = get_coordinator().status().get(agent_name, {})
    if status is None:
        return {"agent": agent_name, "running": coord_status.get("running"),
                "safe_mode": coord_status.get("safe_mode")}
    out = status.to_jsonable()
    out["running"] = coord_status.get("running", False)
    out["safe_mode"] = coord_status.get("safe_mode", False)
    return out


@api_router.post("/agents/{agent_name}/chat", response_model=ChatResponse)
async def agent_chat(agent_name: str, req: ChatRequest) -> ChatResponse:
    """Talk to a specific agent directly. Each agent has its own system
    prompt and tools, so the conversation is genuinely with that
    specialised role — not a router."""
    if agent_name not in _AGENT_NAMES:
        raise HTTPException(404, f"Unknown agent: {agent_name}")
    if not get_vault().is_unlocked:
        return ChatResponse(
            reply="The credential vault is locked. Open Setup to unlock it.",
            safe_mode=True,
        )
    agent = get_coordinator().get(_AGENT_NAMES[agent_name])
    reply = await agent.think(req.message)
    return ChatResponse(reply=reply, safe_mode=agent.safe_mode)


# ============================================================================
# Per-agent memory + chat history (persistent across restarts)
# ============================================================================

_VALID_MEMORY_AGENTS = set(_AGENT_NAMES.keys()) | {"shared"}


@api_router.get("/agents/{agent_name}/memory")
async def list_memory(agent_name: str, include_shared: bool = True,
                       pinned_only: bool = False, limit: int = 200) -> dict:
    if agent_name not in _VALID_MEMORY_AGENTS:
        raise HTTPException(404, f"Unknown agent: {agent_name}")
    from atlas.db.managers import MemoryManager
    rows = MemoryManager.list_for(agent_name,
                                   include_shared=include_shared,
                                   pinned_only=pinned_only,
                                   limit=limit)
    return {
        "agent": agent_name,
        "count": len(rows),
        "memories": [
            {"id": r.id, "agent": r.agent, "content": r.content,
              "pinned": bool(r.pinned), "tags": r.tags or [],
              "source": r.source,
              "created_at": r.created_at.isoformat(),
              "updated_at": r.updated_at.isoformat()}
            for r in rows
        ],
    }


@api_router.post("/agents/{agent_name}/memory")
async def add_memory(agent_name: str, body: dict) -> dict:
    """Add a memory directly via the dashboard (no chat needed). Pass
    agent_name='shared' to write to the shared bucket every agent sees."""
    if agent_name not in _VALID_MEMORY_AGENTS:
        raise HTTPException(404, f"Unknown agent: {agent_name}")
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "content is required")
    pinned = bool(body.get("pinned", False))
    tags = body.get("tags") or []
    if not isinstance(tags, list):
        raise HTTPException(400, "tags must be a list of strings")
    from atlas.db.managers import MemoryManager
    mid = MemoryManager.add(agent_name, content, tags=tags,
                              pinned=pinned, source="api")
    return {"id": mid, "agent": agent_name, "pinned": pinned}


@api_router.delete("/agents/{agent_name}/memory/{memory_id}")
async def delete_memory(agent_name: str, memory_id: int) -> dict:
    from atlas.db.managers import MemoryManager
    m = MemoryManager.get(memory_id)
    if m is None or m.agent != agent_name:
        raise HTTPException(404, "memory not found for this agent")
    MemoryManager.delete(memory_id)
    return {"ok": True}


@api_router.patch("/agents/{agent_name}/memory/{memory_id}")
async def update_memory(agent_name: str, memory_id: int, body: dict) -> dict:
    from atlas.db.managers import MemoryManager
    m = MemoryManager.get(memory_id)
    if m is None or m.agent != agent_name:
        raise HTTPException(404, "memory not found for this agent")
    MemoryManager.update(memory_id,
                          content=body.get("content"),
                          pinned=body.get("pinned"),
                          tags=body.get("tags"))
    return {"ok": True}


@api_router.get("/agents/{agent_name}/chat-history")
async def chat_history(agent_name: str, limit: int = 20) -> dict:
    if agent_name not in _AGENT_NAMES:
        raise HTTPException(404, f"Unknown agent: {agent_name}")
    from atlas.db.managers import ChatHistoryManager
    rows = ChatHistoryManager.recent(agent_name, limit=max(1, min(200, limit)))
    return {
        "agent": agent_name,
        "count": len(rows),
        "turns": [
            {"id": r.id, "role": r.role, "content": r.content,
              "created_at": r.created_at.isoformat()}
            for r in rows
        ],
    }


@api_router.delete("/agents/{agent_name}/chat-history")
async def clear_chat_history(agent_name: str) -> dict:
    if agent_name not in _AGENT_NAMES:
        raise HTTPException(404, f"Unknown agent: {agent_name}")
    from atlas.db.managers import ChatHistoryManager
    n = ChatHistoryManager.clear(agent_name)
    return {"cleared": n}
