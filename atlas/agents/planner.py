"""Planner agent — builds the nightly target list.

Phase 1 behaviour (this file):
  - On startup and every 30 minutes, walk every ACTIVE campaign, look at its
    targets, compute current alt/az + airmass from the configured site, and
    build a sorted list of currently-visible candidates.
  - Persist the plan to in-memory state for the dashboard.
  - Broadcast a `plan_update` event so the Agent Activity feed shows it.
  - Reply to REVISION_REQUEST messages from the Operator by rebuilding the
    same plan immediately.

Phase 2 TODOs (clearly marked in the body):
  - Tonight-window scoping (compute astronomical dusk/dawn instead of
    "above horizon right now").
  - Meridian-flip awareness.
  - Campaign-cadence weighting (every-clear-night vs weekly).
  - NINA sequence XML emission.
  - Per-filter exposure plans.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from atlas.agents.base import BaseAgent
from atlas.agents.state import get_state
from atlas.astronomy import compute_alt_az, airmass, night_window
from atlas.astronomy.catalog import best_now
from atlas.db.managers import (
    CampaignManager, ConfigManager,
)
from atlas.db.models import AgentMessageKind, AgentName
from atlas.db.session import get_session
from atlas.db.models import CampaignTarget, Target


PLAN_REBUILD_INTERVAL_S = 30 * 60   # 30 minutes


class Planner(BaseAgent):
    name = AgentName.PLANNER

    def __init__(self) -> None:
        super().__init__()
        self._last_rebuild = 0.0
        self._initial_done = False
        from atlas.agents.planner_tools import PLANNER_TOOLS
        for spec in PLANNER_TOOLS:
            self.register_tool(spec)

    async def run(self) -> None:
        self.log.info("Planner agent online")
        self.set_task("planner online — building first nightly plan",
                      state="working")
        while not self.should_stop:
            # Force an initial rebuild on startup so the Plan tab has data
            if not self._initial_done:
                self._initial_done = True
                try:
                    await self._rebuild_plan(reason="startup")
                except Exception:
                    self.log.exception("Initial plan rebuild failed")
                self._last_rebuild = asyncio.get_event_loop().time()

            # Drain bus
            msg = await self.recv_with_timeout(timeout_s=10.0)
            if msg is None:
                # Idle — maybe time for a periodic rebuild
                now = asyncio.get_event_loop().time()
                if now - self._last_rebuild >= PLAN_REBUILD_INTERVAL_S:
                    try:
                        await self._rebuild_plan(reason="periodic")
                    except Exception:
                        self.log.exception("Periodic plan rebuild failed")
                    self._last_rebuild = now
                else:
                    # Idle wait — update next-tick estimate for dashboard
                    self._publish_next_tick(now)
                continue

            if msg.kind == AgentMessageKind.REVISION_REQUEST:
                await self._handle_revision(msg)
            elif msg.kind == AgentMessageKind.CANDIDATE_TARGET:
                # Oracle (or another agent) proposes a target. Log + rebuild.
                self.set_task(
                    f"received candidate target — {(msg.payload or {}).get('summary', '')[:60]}",
                    state="working")
                self.log_decision("candidate_received",
                                    inputs={"sender": str(msg.sender),
                                              "payload": msg.payload},
                                    rationale="Phase-1 stub: log + rebuild plan",
                                    session_id=msg.session_id)
                try:
                    await self._rebuild_plan(reason="candidate_target")
                except Exception:
                    self.log.exception("Plan rebuild on candidate failed")
            else:
                await self.handle_relayed_message(msg)

    def _publish_next_tick(self, now_monotonic: float) -> None:
        from datetime import datetime, timedelta
        next_s = max(0.0, PLAN_REBUILD_INTERVAL_S - (now_monotonic - self._last_rebuild))
        nxt = datetime.utcnow() + timedelta(seconds=next_s)
        get_state().update_agent_status(
            "planner",
            next_tick_at=nxt.isoformat(timespec="seconds") + "Z",
            next_tick_kind="rebuild",
        )

    async def _handle_revision(self, msg) -> None:
        self.log.info("Revision requested by %s", msg.sender)
        await self._rebuild_plan(reason=f"revision_request:{msg.sender}")
        self.log_decision("plan_revised", inputs={"details": msg.payload},
                            rationale="Rebuilt plan on revision request",
                            session_id=msg.session_id)

    async def _rebuild_plan(self, *, reason: str) -> None:
        self.set_task(f"rebuilding plan ({reason})", state="working")
        site = ConfigManager.get_site()
        if site is None:
            self.set_task("rebuild_plan: no site config — skipping",
                          state="idle")
            self.log.debug("rebuild_plan: no site config; skipping")
            return

        lat = float(site.latitude)
        lon = float(site.longitude)
        horizon_alt = float(site.horizon_alt_min)
        now = datetime.utcnow()

        # Compute tonight's dark window so the plan is meaningfully
        # bounded — no point listing a target that's only up at noon.
        self.set_task("rebuilding plan — computing dusk/dawn for tonight",
                      state="working")
        nw = night_window(lat, lon, now, altitude_deg=-12.0)
        if nw is None:
            window = None
            mid_night = now  # fall back to "right now" assessment
        else:
            dusk, dawn = nw
            window = {"dusk_utc": dusk.isoformat(timespec="seconds") + "Z",
                       "dawn_utc": dawn.isoformat(timespec="seconds") + "Z",
                       "hours": round((dawn - dusk).total_seconds() / 3600, 1)}
            # Pick mid-night for visibility assessment so we rank targets
            # by their best-case position during the imaging window, not
            # their current daytime position.
            mid_night = dusk + (dawn - dusk) / 2

        # Pull active campaign targets
        campaigns = CampaignManager.list_active()
        self.set_task(
            f"rebuilding plan — checking {len(campaigns)} active campaign(s)",
            state="working")

        visible: list[dict] = []
        skipped_below_horizon = 0
        skipped_no_coords = 0

        with get_session() as s:
            for camp in campaigns:
                rows = s.query(CampaignTarget, Target).join(
                    Target, CampaignTarget.target_id == Target.id
                ).filter(CampaignTarget.campaign_id == camp.id).all()
                for ct, tgt in rows:
                    if tgt.ra_deg is None or tgt.dec_deg is None:
                        skipped_no_coords += 1
                        continue
                    alt, az = compute_alt_az(
                        ra_deg=float(tgt.ra_deg), dec_deg=float(tgt.dec_deg),
                        latitude_deg=lat, longitude_deg=lon,
                        when_utc=mid_night,
                    )
                    if alt < horizon_alt:
                        skipped_below_horizon += 1
                        continue
                    visible.append({
                        "source": "campaign",
                        "campaign_id": camp.id,
                        "campaign_name": camp.name,
                        "workflow": camp.workflow.value if hasattr(camp.workflow, "value") else str(camp.workflow),
                        "priority": camp.priority,
                        "target_id": tgt.id,
                        "target_name": tgt.name,
                        "object_type": tgt.object_type,
                        "ra_deg": float(tgt.ra_deg),
                        "dec_deg": float(tgt.dec_deg),
                        "magnitude": tgt.magnitude,
                        "alt_deg": round(alt, 1),
                        "az_deg": round(az, 1),
                        "airmass": (round(airmass(alt), 2) if airmass(alt) is not None else None),
                    })

        # Seasonal catalog fallback: when no campaign targets are visible,
        # generate a "showcase tonight" list from the built-in catalog so
        # the Planner is never empty-handed. Each entry is tagged source
        # = "seasonal_catalog" so the dashboard can label it accordingly.
        from_catalog: list[dict] = []
        if not visible:
            self.set_task(
                "rebuilding plan — no campaign targets visible, falling back to seasonal catalog",
                state="working")
            entries = best_now(month=now.month, limit=20)
            for e in entries:
                alt, az = compute_alt_az(
                    ra_deg=e["ra_deg"], dec_deg=e["dec_deg"],
                    latitude_deg=lat, longitude_deg=lon, when_utc=mid_night,
                )
                if alt < horizon_alt:
                    continue
                from_catalog.append({
                    "source": "seasonal_catalog",
                    "campaign_id": None,
                    "campaign_name": "(seasonal showcase)",
                    "workflow": "deepsky",
                    "priority": int(50 + max(0, 6 - e.get("magnitude", 10)) * 5),
                    "target_id": None,
                    "target_name": e["name"],
                    "alt_names": e.get("alt_names", []),
                    "object_type": e["object_type"],
                    "ra_deg": e["ra_deg"],
                    "dec_deg": e["dec_deg"],
                    "magnitude": e["magnitude"],
                    "notes": e.get("notes", ""),
                    "alt_deg": round(alt, 1),
                    "az_deg": round(az, 1),
                    "airmass": (round(airmass(alt), 2) if airmass(alt) is not None else None),
                })

        full = (visible or from_catalog)
        full.sort(key=lambda x: (-x["priority"], -x["alt_deg"]))

        plan = {
            "built_at": now.isoformat(timespec="seconds") + "Z",
            "reason": reason,
            "active_campaigns": len(campaigns),
            "visible_targets": full,
            "skipped_below_horizon": skipped_below_horizon,
            "skipped_no_coords": skipped_no_coords,
            "horizon_alt_min_deg": horizon_alt,
            "window": window,
            "fallback_to_catalog": not visible and bool(from_catalog),
            # TODO Phase 2: NINA sequence XML, meridian-flip annotations,
            # cadence weighting, per-target exposure plans.
        }
        get_state().set_tonight_plan(plan)

        await self.bus.broadcast_event({
            "type": "plan_update",
            "sender": "planner",
            "kind": "plan_rebuild",
            "visible": len(full),
            "active_campaigns": len(campaigns),
            "fallback_to_catalog": plan["fallback_to_catalog"],
            "reason": reason,
            "sent_at": plan["built_at"],
        })
        if plan["fallback_to_catalog"]:
            summary = (f"plan rebuilt — {len(full)} seasonal showcase "
                       f"targets visible (no active campaigns)")
        else:
            summary = (f"plan rebuilt — {len(full)} target(s) from "
                       f"{len(campaigns)} active campaign(s)")
        self.set_task(summary + "; next sweep in ~30 min", state="waiting")
        self.log.info(summary)

    async def safe_mode_step(self) -> None:
        # Planner doesn't talk to Claude in this phase, so safe mode is a no-op.
        await asyncio.sleep(30)
