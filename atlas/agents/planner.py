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
from atlas.astronomy import compute_alt_az, airmass
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

    async def run(self) -> None:
        self.log.info("Planner agent online")
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
                continue

            if msg.kind == AgentMessageKind.REVISION_REQUEST:
                await self._handle_revision(msg)
            else:
                self.log.debug("Planner ignoring kind: %s", msg.kind)

    async def _handle_revision(self, msg) -> None:
        self.log.info("Revision requested by %s", msg.sender)
        await self._rebuild_plan(reason=f"revision_request:{msg.sender}")
        self.log_decision("plan_revised", inputs={"details": msg.payload},
                            rationale="Rebuilt plan on revision request",
                            session_id=msg.session_id)

    async def _rebuild_plan(self, *, reason: str) -> None:
        site = ConfigManager.get_site()
        if site is None:
            self.log.debug("rebuild_plan: no site config; skipping")
            return

        campaigns = CampaignManager.list_active()
        now = datetime.utcnow()
        horizon_alt = float(site.horizon_alt_min)

        visible: list[dict] = []
        skipped_below_horizon = 0
        skipped_no_coords = 0

        # Resolve campaign targets in one DB hit
        with get_session() as s:
            for camp in campaigns:
                # CampaignManager.list_active expunges the rows; reload here
                # to walk the relationships safely.
                rows = s.query(CampaignTarget, Target).join(
                    Target, CampaignTarget.target_id == Target.id
                ).filter(CampaignTarget.campaign_id == camp.id).all()
                for ct, tgt in rows:
                    if tgt.ra_deg is None or tgt.dec_deg is None:
                        skipped_no_coords += 1
                        continue
                    alt, az = compute_alt_az(
                        ra_deg=float(tgt.ra_deg), dec_deg=float(tgt.dec_deg),
                        latitude_deg=float(site.latitude),
                        longitude_deg=float(site.longitude),
                        when_utc=now,
                    )
                    if alt < horizon_alt:
                        skipped_below_horizon += 1
                        continue
                    visible.append({
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

        # Highest priority first, then highest altitude (lower airmass)
        visible.sort(key=lambda x: (-x["priority"], -x["alt_deg"]))

        plan = {
            "built_at": now.isoformat(timespec="seconds") + "Z",
            "reason": reason,
            "active_campaigns": len(campaigns),
            "visible_targets": visible,
            "skipped_below_horizon": skipped_below_horizon,
            "skipped_no_coords": skipped_no_coords,
            "horizon_alt_min_deg": horizon_alt,
            # TODO Phase 2: window = (astronomical dusk, astronomical dawn),
            # nautical/civil twilight bands, meridian-flip annotations,
            # per-target exposure plan + NINA sequence XML.
        }
        get_state().set_tonight_plan(plan)

        await self.bus.broadcast_event({
            "type": "plan_update",
            "sender": "planner",
            "kind": "plan_rebuild",
            "visible": len(visible),
            "active_campaigns": len(campaigns),
            "reason": reason,
            "sent_at": plan["built_at"],
        })
        self.log.info("plan rebuilt (%s): %d visible / %d active campaigns",
                        reason, len(visible), len(campaigns))

    async def safe_mode_step(self) -> None:
        # Planner doesn't talk to Claude in this phase, so safe mode is a no-op.
        await asyncio.sleep(30)
