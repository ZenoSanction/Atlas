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


# ============================================================================
# Weather + GO/NO-GO verdict
# ============================================================================

@api_router.get("/weather/current")
async def weather_current() -> dict:
    """Live current-conditions snapshot from Open-Meteo at the configured site."""
    site = ConfigManager.get_site()
    if site is None:
        raise HTTPException(409, "Site coordinates not configured. Open Setup.")
    from atlas.weather.openmeteo import OpenMeteoClient
    try:
        client = OpenMeteoClient(latitude=float(site.latitude),
                                  longitude=float(site.longitude))
        snap = await client.current()
    except Exception as e:
        raise HTTPException(502, f"Open-Meteo request failed: {e}")
    from atlas.units import (
        c_to_f, c_delta_to_f, ms_to_mph, mm_to_in, hpa_to_inhg,
    )
    return {
        "observed_at": snap.observed_at,
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
async def weather_forecast(hours: int = 24, nighttime_only: bool = True) -> dict:
    """Hourly forecast from Open-Meteo.

    Default 24 hours with nighttime_only=True so the dashboard sees only
    the imaging-usable window (sun below -12°). Set nighttime_only=false
    to get every hour back."""
    hours = max(1, min(48, int(hours)))
    site = ConfigManager.get_site()
    if site is None:
        raise HTTPException(409, "Site coordinates not configured. Open Setup.")
    from atlas.weather.openmeteo import OpenMeteoClient
    try:
        client = OpenMeteoClient(latitude=float(site.latitude),
                                  longitude=float(site.longitude))
        rows = await client.forecast_hours(hours=hours)
    except Exception as e:
        raise HTTPException(502, f"Open-Meteo request failed: {e}")

    # Optional nighttime filter — keep only hours where the sun is below
    # -12° at that time (nautical twilight + darker).
    night_meta = None
    if nighttime_only:
        from atlas.astronomy import sun_altitude, night_window
        nw = night_window(float(site.latitude), float(site.longitude),
                            datetime.utcnow(), altitude_deg=-12.0)
        if nw is not None:
            dusk, dawn = nw
            night_meta = {
                "dusk_utc": dusk.isoformat(timespec="seconds") + "Z",
                "dawn_utc": dawn.isoformat(timespec="seconds") + "Z",
                "hours": round((dawn - dusk).total_seconds() / 3600, 2),
            }
        kept: list[dict] = []
        for r in rows:
            try:
                t = datetime.fromisoformat(r["time"])
            except Exception:
                continue
            if sun_altitude(float(site.latitude), float(site.longitude), t) < -12.0:
                kept.append(r)
        rows = kept
    from atlas.units import c_to_f, c_delta_to_f, ms_to_mph, mm_to_in
    out_rows = []
    for r in rows:
        dm_c = r["temperature_c"] - r["dew_point_c"]
        out_rows.append({
            "time_utc": r["time"],
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


# ============================================================================
# Weather thresholds (Setup tab)
# ============================================================================

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
        agents[name] = d
    verdict = st.get_verdict()
    return {
        "now_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "simulation_mode": settings.simulation_mode,
        "observatory_name": (site.observatory_name if site else None),
        "verdict": verdict.to_jsonable() if verdict else None,
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
