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
from datetime import datetime, timedelta
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

# Session shaping (operator-settable; defaults below match the policy
# decided 2026-05-21 — "depth over breadth"):
#
#   MAX_TARGETS_PER_SESSION = 4
#       Hard ceiling on how many targets a single night can include.
#       Fewer targets means each one gets meaningful integration time
#       rather than a tour of 12 ~10-min snapshots.
#
#   MIN_DWELL_MINUTES = 60
#       Floor on actual imaging time per target. If a target's default
#       exposure plan is shorter than this (e.g. some astrometry plans
#       are only 5 min), the dwell is padded to 60 min by repeating
#       the plan or extending the count.
MAX_TARGETS_PER_SESSION = 4
MIN_DWELL_MINUTES = 60


class Planner(BaseAgent):
    name = AgentName.PLANNER

    def __init__(self) -> None:
        super().__init__()
        self._last_rebuild = 0.0
        self._initial_done = False
        # Constraints injected by Operator decisions (e.g., ["avoid_moon"])
        # — apply on the next rebuild then clear.
        self._active_constraints: list[str] = []
        from atlas.agents.planner_tools import PLANNER_TOOLS
        for spec in PLANNER_TOOLS:
            self.register_tool(spec)

    async def run(self) -> None:
        self.log.info("Planner agent online")
        self.set_task("planner online — building first nightly plan",
                      state="working")

        # Force an initial rebuild on startup so the Plan tab has data
        try:
            await self._rebuild_plan(reason="startup")
        except Exception:
            self.log.exception("Initial plan rebuild failed")
        self._initial_done = True
        self._last_rebuild = asyncio.get_event_loop().time()

        # Background periodic-rebuild task: long sleep cadence, not the
        # main mechanism. Most rebuilds are now triggered by inbound
        # messages (Operator decisions, manual revision requests). This
        # task just catches "still active, nothing has happened, but
        # we should re-check visibility windows" cases.
        periodic_task = asyncio.create_task(self._periodic_rebuild_loop(),
                                              name="planner-periodic")

        try:
            while not self.should_stop:
                # Block on the bus — wake instantly when a message arrives,
                # do nothing in between.
                try:
                    msg = await self.recv()
                except (asyncio.CancelledError, RuntimeError):
                    break

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
        finally:
            periodic_task.cancel()
            try:
                await periodic_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _periodic_rebuild_loop(self) -> None:
        """Sleep-and-check-cache periodic rebuild. Wakes once an hour
        (was 30 minutes of polling) and only actually rebuilds if the
        last rebuild is older than PLAN_REBUILD_INTERVAL_S — most
        nights this loop fires zero or one rebuild because messages
        from the Operator pipeline drove the rebuilds already."""
        # Initial sleep so the startup rebuild from run() has time to land
        await asyncio.sleep(60)
        while not self.should_stop:
            now = asyncio.get_event_loop().time()
            if now - self._last_rebuild >= PLAN_REBUILD_INTERVAL_S:
                try:
                    await self._rebuild_plan(reason="periodic")
                except Exception:
                    self.log.exception("Periodic plan rebuild failed")
                self._last_rebuild = asyncio.get_event_loop().time()
            self._publish_next_tick(asyncio.get_event_loop().time())
            # Sleep until just past the next theoretical rebuild boundary.
            await asyncio.sleep(max(60.0, PLAN_REBUILD_INTERVAL_S / 2))

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

    # _handle_session_decision was removed in the 2026-05-21 refactor.
    # The plan is now published READY directly from _rebuild_plan; the
    # Operator only intervenes for hard-stops (storm / equipment risk),
    # at which point it flips the live plan's state to HARD_STOP
    # directly via shared state. There's nothing for the Planner to
    # finalise after the fact — the plan was already final when it was
    # built.

    async def _cancel_session(self, *, reason: str,
                                from_review: dict | None = None) -> None:
        """Terminate the current workflow with a cancellation. Used when:
          - no site config (can't plan anything)
          - zero visible targets (catalog fallback also empty)
          - an Operator-requested replan still yields zero targets
          - explicit operator cancel via the cancel_session tool

        Marks the current SessionReview terminal-cancelled, broadcasts,
        and logs a decision. Does NOT relay to Critic — this is a
        Planner-side early-exit per the operator's workflow:
        "the planner either ends planning for the session, or if
        possible he re-plans"."""
        from atlas.agents.session_workflow import (
            SessionReview, new_review_id, PHASE_PLAN_BUILT, PHASE_CANCELLED,
        )
        from datetime import datetime as _dt

        if from_review is not None:
            review = SessionReview.from_jsonable(from_review)
        else:
            # Fresh review for the cancellation so the dashboard shows the
            # terminal phase + audit trail rather than nothing.
            review = SessionReview(
                review_id=new_review_id(),
                plan={"visible_targets": [], "active_campaigns": 0,
                       "fallback_to_catalog": False,
                       "built_at": _dt.utcnow().isoformat(timespec="seconds") + "Z"},
                started_at=_dt.utcnow().isoformat(timespec="seconds") + "Z",
                phase=PHASE_PLAN_BUILT,
            )
            review.advance(PHASE_PLAN_BUILT, "planner",
                            note="cancellation initiated by Planner")
        review.operator_decision = "cancel"
        review.operator_reason = reason
        review.advance(PHASE_CANCELLED, "planner",
                        note=f"session cancelled by Planner: {reason[:60]}")
        get_state().set_session_review(review.to_jsonable())
        self.log_decision("session_cancelled_by_planner",
                            inputs={"review_id": review.review_id,
                                      "reason": reason},
                            outputs={"phase": "cancelled"},
                            rationale=reason)
        try:
            await self.bus.broadcast_event({
                "type": "session_cancelled",
                "sender": "planner",
                "kind": "cancelled",
                "review_id": review.review_id,
                "reason": reason,
                "sent_at": review.final_at,
            })
        except Exception:
            pass
        self.set_task(f"session {review.review_id} CANCELLED: {reason[:60]}",
                      state="idle")
        self.log.info("Session cancelled by Planner: %s", reason)

    async def _rebuild_plan(self, *, reason: str) -> None:
        self.set_task(f"rebuilding plan ({reason})", state="working")
        site = ConfigManager.get_site()
        if site is None:
            # Site config missing — can't plan anything. End the session
            # rather than silently doing nothing.
            self.log.warning("rebuild_plan: no site config; cancelling session")
            await self._cancel_session(
                reason="No observatory site configured. Open Setup → Site to fix.")
            return

        lat = float(site.latitude)
        lon = float(site.longitude)
        horizon_alt = float(site.horizon_alt_min)
        now = datetime.utcnow()

        # Planner is the ONE consumer that pulls fresh weather. Every
        # other agent reads from the cache without triggering a network
        # call. force_refresh=True here ensures the rebuild gets a
        # current snapshot regardless of when the last pull happened —
        # this is "we're about to commit hardware to ~8 hours of work,
        # let's see the actual sky."
        try:
            from atlas.weather.cache import get_weather_cache
            await get_weather_cache().get(lat=lat, lon=lon,
                                              force_refresh=True)
        except Exception as e:
            self.log.warning("rebuild_plan: weather force-refresh failed "
                              "(%s) — proceeding with whatever's cached", e)

        # Classify where we are in the 24-hour cycle. The Planner's
        # behaviour is meaningfully different depending on whether
        # we're calling _rebuild_plan in the middle of the day (plan
        # for tonight), during evening twilight (final plan + pre-
        # flight prep), in astronomical dark (mid-night replan after
        # conditions cleared), or in morning twilight (start
        # preparing tomorrow's plan once the day rolls over).
        from atlas.astronomy.day_phase import (
            current_phase, PHASE_ASTRO_DARK, PHASE_EVENING_TWILIGHT,
            PHASE_MORNING_TWILIGHT, PHASE_DAY,
        )
        day_phase = current_phase(lat, lon, now)
        self.set_task(
            f"rebuilding plan — phase={day_phase.phase}, "
            f"sun {day_phase.sun_altitude_deg:.1f}°",
            state="working",
        )

        # Compute the relevant astronomical-dark window for THIS plan.
        # day_phase already knows the next or current dark window;
        # we use the wider -12° nautical window for visibility ranking
        # because targets often need a few minutes of nautical-twilight
        # shoulder for slew + plate-solve before astronomical dark
        # itself.
        nw = night_window(lat, lon,
                            now - timedelta(hours=12)
                                if day_phase.is_imaging_window else now,
                            altitude_deg=-12.0)
        if nw is None:
            window = None
            mid_night = now
        else:
            dusk, dawn = nw
            # Effective window start: dusk for daytime rebuilds,
            # now for mid-night rebuilds (the storm-cleared case).
            effective_start = max(dusk, now)
            window = {
                "dusk_utc": dusk.isoformat(timespec="seconds") + "Z",
                "dawn_utc": dawn.isoformat(timespec="seconds") + "Z",
                "effective_start_utc": effective_start.isoformat(timespec="seconds") + "Z",
                "hours": round((dawn - dusk).total_seconds() / 3600, 1),
                "remaining_hours": round(
                    max(0.0, (dawn - effective_start).total_seconds() / 3600), 1
                ),
                "day_phase": day_phase.phase,
                "is_imaging_window_now": day_phase.is_imaging_window,
            }
            # Visibility ranking time: midpoint of the EFFECTIVE window,
            # so a 1 AM replan ranks targets by where they are 1 AM-to-
            # dawn-midpoint, not by where they were at 23:00 last night.
            mid_night = effective_start + (dawn - effective_start) / 2

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
                    # Cadence weighting: if this campaign hasn't run in the
                    # last N days (per its cadence), bump its priority. Rough
                    # cadence-to-days map; refined in a later pass.
                    cadence_str = (camp.cadence or "every_clear_night").lower()
                    cadence_days = {
                        "every_clear_night": 1,
                        "every_night": 1,
                        "nightly": 1,
                        "every_other_night": 2,
                        "weekly": 7,
                        "biweekly": 14,
                        "monthly": 30,
                    }.get(cadence_str, 1)
                    bump = 0
                    try:
                        # Look at the most-recent frame this target produced
                        from atlas.db.models import Frame
                        last = (s.query(Frame.captured_at)
                                  .filter(Frame.target_id == tgt.id)
                                  .order_by(Frame.captured_at.desc())
                                  .first())
                        if last is None or last[0] is None:
                            bump = 15   # never observed yet
                        else:
                            from datetime import datetime as _dt
                            days_since = (_dt.utcnow() - last[0]).days
                            if days_since > cadence_days:
                                bump = min(20, days_since - cadence_days)
                    except Exception:
                        pass

                    from atlas.agents.exposure_plan import default_plan_for, total_integration_min
                    wf = camp.workflow.value if hasattr(camp.workflow, "value") else str(camp.workflow)
                    equip = ConfigManager.get_equipment()
                    cam_type = (equip.camera_type if equip else "OSC")
                    exp_plan = default_plan_for(wf, cam_type)

                    visible.append({
                        "source": "campaign",
                        "campaign_id": camp.id,
                        "campaign_name": camp.name,
                        "workflow": wf,
                        "priority": int(camp.priority) + bump,
                        "cadence_bump": bump,
                        "cadence": cadence_str,
                        "exposure_plan": exp_plan,
                        "total_integration_min": total_integration_min(exp_plan),
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
                from atlas.agents.exposure_plan import default_plan_for, total_integration_min
                equip = ConfigManager.get_equipment()
                cam_type = (equip.camera_type if equip else "OSC")
                exp_plan = default_plan_for("deepsky", cam_type)
                from_catalog.append({
                    "source": "seasonal_catalog",
                    "campaign_id": None,
                    "campaign_name": "(seasonal showcase)",
                    "workflow": "deepsky",
                    "priority": int(50 + max(0, 6 - e.get("magnitude", 10)) * 5),
                    "cadence_bump": 0,
                    "cadence": "showcase",
                    "exposure_plan": exp_plan,
                    "total_integration_min": total_integration_min(exp_plan),
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

        # Apply Operator-supplied constraints from the last session decision.
        # Currently supported: 'avoid_moon' filters targets within 40° of the
        # moon when it's above the horizon and >30% illuminated.
        applied_constraints: list[str] = []
        if "avoid_moon" in self._active_constraints:
            try:
                from atlas.astronomy import angular_separation, moon_position
                m_ra, m_dec, illum = moon_position(mid_night)
                m_alt, _ = compute_alt_az(m_ra, m_dec, lat, lon, mid_night)
                if m_alt > 0 and illum > 0.30:
                    before = len(full)
                    full = [t for t in full
                              if angular_separation(t["ra_deg"], t["dec_deg"],
                                                       m_ra, m_dec) >= 40.0]
                    if before != len(full):
                        applied_constraints.append(
                            f"avoid_moon (dropped {before - len(full)} target(s))")
            except Exception:
                self.log.exception("avoid_moon filter failed")

        full.sort(key=lambda x: (-x["priority"], -x["alt_deg"]))
        full_unshaped = list(full)   # full ranked list, for the
                                       # dashboard's "considered but not
                                       # scheduled" view

        # ----------------------------------------------------------------
        # Time-aware scheduler: queue by when each target enters viewing
        # range. Pad each entry's exposure plan to >= MIN_DWELL_MINUTES
        # first so the scheduler has the actual preferred dwell on hand.
        # Then walk dusk -> dawn and slot targets in by rise-time, with
        # priority as tiebreaker. Targets that wouldn't fit a full dwell
        # are dropped entirely (depth-over-breadth), not half-imaged.
        # ----------------------------------------------------------------
        for t in full:
            current_min = float(t.get("total_integration_min") or 0.0)
            if current_min < MIN_DWELL_MINUTES and t.get("exposure_plan"):
                pad_factor = MIN_DWELL_MINUTES / max(current_min, 1.0)
                padded_plan = []
                new_total = 0.0
                for set_ in t["exposure_plan"]:
                    # Scale count, not exposure_s — preserves SNR + dither cadence.
                    new_count = max(1, int(round(set_["count"] * pad_factor)))
                    new_total_s = set_["exposure_s"] * new_count
                    padded_plan.append({
                        "filter": set_["filter"],
                        "exposure_s": set_["exposure_s"],
                        "count": new_count,
                        "total_s": new_total_s,
                        "total_min": round(new_total_s / 60.0, 1),
                    })
                    new_total += new_total_s / 60.0
                t["exposure_plan"] = padded_plan
                t["total_integration_min"] = round(new_total, 1)
                t["dwell_padded_from_min"] = round(current_min, 1)
            t["min_dwell_minutes"] = MIN_DWELL_MINUTES

        scheduled: list[dict] = []
        unscheduled: list[dict] = []
        sched_total_min = 0.0
        if window is None:
            # No dark window (polar day) — nothing schedulable. The
            # session-cancel paths elsewhere handle this.
            window_min = None
            overrun = False
            schedule_obj = None
        else:
            from atlas.astronomy.scheduler import schedule_targets
            dusk_dt = datetime.fromisoformat(window["dusk_utc"].rstrip("Z"))
            dawn_dt = datetime.fromisoformat(window["dawn_utc"].rstrip("Z"))
            # window_min is the EFFECTIVE remaining-imaging time, not
            # the full original night. A mid-night rebuild shows the
            # smaller number — that's what the operator cares about.
            window_min = window["remaining_hours"] * 60.0
            schedule_obj = schedule_targets(
                full,
                lat=lat, lon=lon, horizon_alt=horizon_alt,
                dusk=dusk_dt, dawn=dawn_dt,
                max_targets=MAX_TARGETS_PER_SESSION,
                min_dwell_minutes=MIN_DWELL_MINUTES,
                fit_strategy="depth",
                now_utc=now,
            )
            for slot in schedule_obj.slots:
                t = dict(slot.target)
                t["start_utc"] = slot.start_utc.isoformat(timespec="seconds") + "Z"
                t["end_utc"] = slot.end_utc.isoformat(timespec="seconds") + "Z"
                t["scheduled_for_min"] = slot.dwell_min
                if slot.truncated_from_min:
                    t["scheduled_truncated_from_min"] = slot.truncated_from_min
                if slot.visibility:
                    t["visible_from_utc"] = slot.visibility.visible_from.isoformat(timespec="seconds") + "Z"
                    t["visible_until_utc"] = slot.visibility.visible_until.isoformat(timespec="seconds") + "Z"
                    t["peak_alt_deg"] = slot.visibility.peak_alt_deg
                scheduled.append(t)
            unscheduled = [{
                "target_name": s["target"].get("target_name"),
                "campaign_name": s["target"].get("campaign_name"),
                "priority": s["target"].get("priority"),
                "reason": s["reason"],
            } for s in schedule_obj.skipped]
            sched_total_min = schedule_obj.scheduled_total_min
            overrun = False   # by construction the scheduler can't overrun

        plan = {
            "built_at": now.isoformat(timespec="seconds") + "Z",
            "reason": reason,
            "active_campaigns": len(campaigns),
            # The Operator + UI both read `visible_targets`; it's the
            # scheduled queue, time-ordered, each entry carrying start_utc
            # / end_utc / scheduled_for_min.
            "visible_targets": scheduled,
            "considered": full_unshaped,
            "considered_count": len(full_unshaped),
            "unscheduled": unscheduled,
            "max_targets_per_session": MAX_TARGETS_PER_SESSION,
            "min_dwell_minutes": MIN_DWELL_MINUTES,
            "scheduled_total_min": round(sched_total_min, 1),
            "dark_window_min": (round(window_min, 1) if window_min else None),
            "overruns_dark_window": bool(overrun),
            "fit_strategy": "depth",
            "skipped_below_horizon": skipped_below_horizon,
            "skipped_no_coords": skipped_no_coords,
            "horizon_alt_min_deg": horizon_alt,
            "window": window,
            "day_phase": day_phase.to_jsonable(),
            "fallback_to_catalog": not visible and bool(from_catalog),
            "applied_constraints": applied_constraints,
        }
        get_state().set_tonight_plan(plan)

        await self.bus.broadcast_event({
            "type": "plan_update",
            "sender": "planner",
            "kind": "plan_rebuild",
            "visible": len(scheduled),
            "considered": len(full_unshaped),
            "active_campaigns": len(campaigns),
            "fallback_to_catalog": plan["fallback_to_catalog"],
            "scheduled_total_min": round(sched_total_min, 1),
            "dark_window_min": (round(window_min, 1) if window_min else None),
            "overruns_dark_window": bool(overrun),
            "reason": reason,
            "sent_at": plan["built_at"],
        })
        n_sched = len(scheduled)
        n_cons  = len(full_unshaped)
        budget_note = ""
        if window_min is not None:
            budget_note = (f"; scheduled {sched_total_min:.0f} min "
                            f"of {window_min:.0f} min dark "
                            f"(fits depth-first, cap {MAX_TARGETS_PER_SESSION})")
        if plan["fallback_to_catalog"]:
            summary = (f"plan rebuilt -- {n_sched} of {n_cons} seasonal "
                       f"showcase targets scheduled "
                       f"(>={MIN_DWELL_MINUTES} min each){budget_note}")
        else:
            summary = (f"plan rebuilt -- {n_sched} of {n_cons} target(s) "
                       f"scheduled (>={MIN_DWELL_MINUTES} min each) from "
                       f"{len(campaigns)} active campaign(s){budget_note}")
        self.set_task(summary + "; next sweep in ~30 min", state="waiting")
        self.log.info(summary)

        # If the plan ended up empty — no active campaigns produced
        # visible targets, the seasonal catalog also returned nothing —
        # there's no point in relaying to the Critic. End the session
        # here per the operator's workflow ("the planner either ends
        # planning for the session, or if possible he re-plans").
        if not full:
            constraint_note = ""
            if applied_constraints:
                constraint_note = (f" after applying {', '.join(applied_constraints)}"
                                     if applied_constraints else "")
            empty_reason = (f"No visible targets for tonight{constraint_note}. "
                              f"Active campaigns: {len(campaigns)}, "
                              f"skipped below horizon: {skipped_below_horizon}, "
                              f"skipped no coords: {skipped_no_coords}.")
            await self._cancel_session(reason=empty_reason)
            return

        # Plan is READY the moment it's built. No multi-stage gated
        # approval pipeline — the operator wants the plan immediately,
        # with checks running asynchronously as advisories.
        try:
            from atlas.agents.session_workflow import (
                SessionPlanState, new_review_id, STATE_READY,
            )
            top_names = [t["target_name"] for t in scheduled[:5]]
            review = SessionPlanState(
                review_id=new_review_id(),
                plan=plan,
                started_at=plan["built_at"],
                state=STATE_READY,
            )
            # Publish READY immediately so the dashboard's Plan tab can
            # render. Critic + Oracle will append advisories afterward.
            get_state().set_session_review(review.to_jsonable())
            # Fire-and-forget advisory requests in parallel. Critic
            # checks weather + moon + hardware; Oracle checks for
            # revisit candidates + extended-integration suggestions.
            # Each agent appends its findings to the plan's advisories
            # via its own handler — the Planner doesn't wait.
            await self.send(
                AgentName.CRITIC, AgentMessageKind.STATUS,
                payload={
                    "summary": (f"Plan rebuilt ({reason}) — {len(scheduled)} "
                                  f"target(s). Top: "
                                  f"{', '.join(top_names) if top_names else '(none)'}. "
                                  "Advisory review please."),
                    "kind": "plan_advisory_request",
                    "review_id": review.review_id,
                    "review": review.to_jsonable(),
                    "from_chat": False,
                },
            )
            await self.send(
                AgentName.ORACLE, AgentMessageKind.STATUS,
                payload={
                    "summary": (f"Plan rebuilt ({reason}). "
                                  "Advisory check for revisit + extended-integration "
                                  "candidates."),
                    "kind": "plan_advisory_request",
                    "review_id": review.review_id,
                    "review": review.to_jsonable(),
                    "from_chat": False,
                },
            )
        except Exception:
            self.log.exception("Failed to request advisory reviews")

    async def safe_mode_step(self) -> None:
        # Planner doesn't talk to Claude in this phase, so safe mode is a no-op.
        await asyncio.sleep(30)
