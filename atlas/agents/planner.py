"""Planner agent — builds the nightly target list.

CORE PRINCIPLE (operator-stated, 2026-05-24):
    ALWAYS the session plan is created. ALWAYS.
    Just because there is a hold on execution does not mean to not
    create a plan. The plan could be used the next night that is
    clear for a session. The plan if never created never gets used.
    If a plan is only created when it is a GO situation, then there
    is no reason to have a planner. Everything starts with the plan.

Implementation guarantee: every entry point into the Planner's plan
production (startup, periodic, REVISION_REQUEST, CANDIDATE_TARGET,
hand-bound tools) is wrapped so that EVEN ON EXCEPTION the
tonight_plan + session_review slots are populated with at least an
empty plan + an advisory explaining why it's empty. No weather, no
verdict, no execution block, no missing-site condition can leave the
Plan tab empty.

Plan production is fully independent of:
  - OperatorVerdict (GO / CAUTION / NO-GO)
  - Execution-block state
  - Cloud forecast advisories
  - Weather data freshness
  - Whether a session is currently running

The plan reflects WHAT WOULD BE IDEAL TO IMAGE TONIGHT given the
site + active campaigns. Whether tonight is observable is a separate
decision tracked by OperatorVerdict and gated by the verdict watcher.
The plan persists across multi-night campaigns; tomorrow's plan
builds on tonight's accumulated frames via the continuity module.

Phase 1 behaviour:
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


PLAN_REBUILD_INTERVAL_S = 60 * 60   # 60 minutes — was 30; periodic is a
                                       # safety net, not the main mechanism.
                                       # Real triggers (REVISION_REQUEST,
                                       # CANDIDATE_TARGET, operator commands)
                                       # already fire rebuilds when something
                                       # actually changed. The hourly periodic
                                       # catches "did dark window shift?" etc.

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

        # Force an initial rebuild on startup so the Plan tab has data.
        # Bulletproof: ANY exception still publishes a fallback empty
        # plan with the error as an advisory, so the dashboard never
        # sits with "no plan."
        try:
            await self._rebuild_plan(reason="startup")
        except Exception as e:
            self.log.exception("Initial plan rebuild failed — publishing "
                                 "fallback empty plan with the error as an "
                                 "advisory so the Plan tab is never empty")
            try:
                await self._publish_empty_plan_with_advisory(
                    reason="startup_failed",
                    advisory_kind="planner_error",
                    advisory_severity="critical",
                    advisory_msg=(f"Plan rebuild crashed on startup: "
                                    f"{type(e).__name__}: {e}. "
                                    f"Check the Planner log for the full trace, "
                                    f"then trigger a manual replan."),
                )
            except Exception:
                self.log.exception("Fallback empty-plan publish ALSO failed "
                                     "— this is a serious bug; agent will "
                                     "continue but the Plan tab will be empty")
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

                try:
                    if msg.kind == AgentMessageKind.REVISION_REQUEST:
                        await self._handle_revision(msg)
                    elif msg.kind == AgentMessageKind.CANDIDATE_TARGET:
                        # Oracle (or another agent) proposes a target.
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
                    elif (msg.kind == AgentMessageKind.STATUS
                            and (msg.payload or {}).get("kind") == "plan_review"
                            and (msg.payload or {}).get("phase") == "finalize"):
                        # Stage 5 — chain returns from Oracle. Republish
                        # the plan as FINAL.
                        await self._finalize_review_chain(msg.payload)
                    else:
                        await self.handle_relayed_message(msg)
                    self._mark_msg_handled(msg, ok=True)
                except Exception as e:
                    self.log.exception("Planner failed handling message")
                    self._mark_msg_handled(msg, ok=False,
                                              error=f"{type(e).__name__}: {e}")
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
                except Exception as e:
                    self.log.exception("Periodic plan rebuild failed — "
                                          "publishing fallback empty plan")
                    try:
                        await self._publish_empty_plan_with_advisory(
                            reason="periodic_rebuild_failed",
                            advisory_kind="planner_error",
                            advisory_severity="warning",
                            advisory_msg=(f"Periodic plan rebuild crashed: "
                                            f"{type(e).__name__}: {e}. "
                                            f"Existing plan (if any) was "
                                            f"replaced with this placeholder. "
                                            f"Check the Planner log."),
                        )
                    except Exception:
                        self.log.exception("Periodic fallback publish ALSO "
                                              "failed")
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

    async def _finalize_review_chain(self, payload: dict) -> None:
        """Stage 5 — Oracle returned the chain to us. Republish the
        plan as FINAL.

        All agents have already attached advisories during the chain
        (Critic: weather/moon/hardware; Operator: verdict context;
        Oracle: revisit + extended-integration suggestions). The
        session_review in state already carries them. Our job here:

          1. Re-fetch the live session_review so we see every advisory
          2. Mark review_phase = "final" so the dashboard stops showing
             "chain in progress" and shows "final published"
          3. Broadcast a `plan_finalized` bus event so the Plan tab
             refetches and renders the final state

        We do NOT rebuild the target list at this stage — Oracle's
        suggestions are advisories the operator reads, then if the
        operator wants to incorporate them via the chat ("add M51 to
        tonight"), the Planner does a full new rebuild which kicks
        off a fresh chain. This keeps the chain idempotent: one
        rebuild → one chain → one final publication. Operator-
        directed changes restart the cycle."""
        review_id = payload.get("review_id") or ""
        try:
            get_state().set_review_phase("final", review_id=review_id)
        except Exception:
            pass
        try:
            review = get_state().get_session_review() or {}
            n_advisories = len(review.get("advisories") or [])
            self.set_task(
                f"Stage 5/5: Planner FINAL — {n_advisories} advisor"
                f"{'ies' if n_advisories != 1 else 'y'} "
                f"from review chain; PUBLISHED to Plan tab for human "
                f"examination",
                state="idle",
            )
            try:
                from datetime import datetime as _dt
                await self.bus.broadcast_event({
                    "type": "review_chain_stage",
                    "sender": "planner",
                    "stage": "5/5",
                    "agent": "planner",
                    "phase": "final",
                    "review_id": review_id,
                    "n_advisories": n_advisories,
                    "sent_at": _dt.utcnow().isoformat(timespec="seconds") + "Z",
                })
            except Exception:
                pass
            self.log_decision(
                "review_chain_finalized",
                inputs={"review_id": review_id,
                          "advisories": n_advisories},
                rationale=("Planner→Critic→Operator→Oracle→Planner "
                              "chain complete; plan published as FINAL "
                              "to the Plan tab."),
            )
            await self.bus.broadcast_event({
                "type": "plan_finalized",
                "sender": "planner",
                "review_id": review_id,
                "advisory_count": n_advisories,
                "sent_at": payload.get("sent_at"),
            })
        except Exception:
            self.log.exception("Finalize step had a non-fatal error; "
                                  "review_phase set, plan still in state")

    async def _handle_revision(self, msg) -> None:
        self.log.info("Revision requested by %s", msg.sender)
        # Bulletproof: ANY exception still publishes a fallback empty
        # plan with the error as an advisory. The Plan tab MUST NEVER
        # sit empty after a revision request — that's the operator
        # principle: "Always the session plan is created!!!! Always!!!!"
        try:
            await self._rebuild_plan(reason=f"revision_request:{msg.sender}")
        except Exception as e:
            self.log.exception("Revision rebuild crashed — publishing "
                                 "fallback empty plan with the error as "
                                 "an advisory")
            try:
                await self._publish_empty_plan_with_advisory(
                    reason=f"revision_request_failed:{msg.sender}",
                    advisory_kind="planner_error",
                    advisory_severity="critical",
                    advisory_msg=(f"Revision rebuild crashed: "
                                    f"{type(e).__name__}: {e}. "
                                    f"Plan tab kept populated with this "
                                    f"empty-plan placeholder so the workflow "
                                    f"never stalls. Check the Planner log."),
                )
            except Exception:
                self.log.exception("Fallback empty-plan publish ALSO failed")
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

    async def _publish_empty_plan_with_advisory(self, *,
                                                       reason: str,
                                                       advisory_kind: str,
                                                       advisory_severity: str,
                                                       advisory_msg: str) -> None:
        """Publish a minimal READY plan with one inline advisory.

        Used when the Planner can't compute visibility (no site config,
        etc.) but still must produce a plan so the dashboard never sits
        with "no plan". Per operator principle ("I always want a session
        planned and created no matter the warnings or no-gos") this is
        the catch-all that keeps the workflow moving even on hard
        configuration errors — the human sees the advisory and knows
        exactly what to fix."""
        from atlas.agents.session_workflow import (
            Advisory, SessionPlanState, new_review_id, STATE_READY,
        )
        from datetime import datetime as _dt
        now_str = _dt.utcnow().isoformat(timespec="seconds") + "Z"
        plan = {
            "built_at": now_str,
            "reason": reason,
            "active_campaigns": 0,
            "visible_targets": [],
            "considered": [],
            "considered_count": 0,
            "unscheduled": [],
            "scheduled_total_min": 0.0,
            "dark_window_min": None,
            "overruns_dark_window": False,
            "fit_strategy": "depth",
            "skipped_below_horizon": 0,
            "skipped_no_coords": 0,
            "horizon_alt_min_deg": None,
            "window": None,
            "day_phase": None,
            "fallback_to_catalog": False,
            "applied_constraints": [],
            "blocked_reason": advisory_msg,
        }
        get_state().set_tonight_plan(plan)
        review = SessionPlanState(
            review_id=new_review_id(),
            plan=plan,
            started_at=now_str,
            state=STATE_READY,
        )
        review.add_advisory(Advisory(
            kind=advisory_kind, severity=advisory_severity,
            message=advisory_msg, source="planner", at=now_str,
        ))
        get_state().set_session_review(review.to_jsonable())
        try:
            await self.bus.broadcast_event({
                "type": "plan_update",
                "sender": "planner",
                "kind": "plan_rebuild",
                "visible": 0,
                "considered": 0,
                "active_campaigns": 0,
                "fallback_to_catalog": False,
                "scheduled_total_min": 0.0,
                "dark_window_min": None,
                "overruns_dark_window": False,
                "reason": reason,
                "blocked_reason": advisory_msg,
                "sent_at": now_str,
            })
        except Exception:
            pass
        self.set_task(f"plan published with {advisory_severity} advisory: "
                        f"{advisory_msg[:50]}",
                        state="waiting")
        self.log.info("Published empty plan with advisory: %s", advisory_msg)

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
            # Site config missing — we can't compute visibility, but per
            # the operator's principle ("I always want a session planned
            # and created no matter the warnings or no-gos") we still
            # publish a READY plan with a clear advisory rather than
            # cancelling. The dashboard always shows a plan exists; the
            # human sees exactly why it's empty.
            self.log.warning("rebuild_plan: no site config; publishing "
                              "empty plan with site-missing advisory")
            await self._publish_empty_plan_with_advisory(
                reason=reason,
                advisory_kind="config_missing",
                advisory_severity="critical",
                advisory_msg=("No observatory site configured. "
                                "Open Setup → Site to set latitude / longitude / "
                                "horizon, then trigger a replan."),
            )
            return

        lat = float(site.latitude)
        lon = float(site.longitude)
        horizon_alt = float(site.horizon_alt_min)
        now = datetime.utcnow()

        # Trigger a fresh weather pull as a fire-and-forget background
        # task. The Planner does NOT wait for it. The rebuild proceeds
        # against whatever the cache has RIGHT NOW — adaptive TTL
        # keeps that within 60-90 s during active monitoring, which is
        # current enough for plan-level decisions. The fresh pull
        # lands by the next consumer's read (Critic's next standard
        # loop, ~90 s out, picks it up automatically).
        #
        # Principle (operator request 2026-05-21): no agent waits for
        # IO it can avoid. The plan exists the moment we have data;
        # making it fresher takes a separate, non-blocking path.
        try:
            from atlas.weather.cache import get_weather_cache
            cache = get_weather_cache()
            # Trigger refresh in background — don't await it.
            asyncio.create_task(
                cache.get(lat=lat, lon=lon, force_refresh=True),
                name=f"planner-bgrefresh-{reason}",
            )
            # Read what's in cache right now (non-blocking peek).
            current = cache.peek()
            if current.age_seconds is not None and current.age_seconds > 300:
                self.log.info(
                    "rebuild_plan: cache is %.0fs old; bg-refresh "
                    "kicked off, building with current data",
                    current.age_seconds,
                )
        except Exception as e:
            self.log.warning("rebuild_plan: bg weather refresh failed to "
                              "kick off (%s) — proceeding regardless", e)

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
            equip = ConfigManager.get_equipment()
            mount_type = getattr(equip, "mount_type", "gem") if equip else "gem"
            schedule_obj = schedule_targets(
                full,
                lat=lat, lon=lon, horizon_alt=horizon_alt,
                dusk=dusk_dt, dawn=dawn_dt,
                max_targets=MAX_TARGETS_PER_SESSION,
                min_dwell_minutes=MIN_DWELL_MINUTES,
                fit_strategy="depth",
                now_utc=now,
                mount_type=mount_type,
            )
            for slot in schedule_obj.slots:
                t = dict(slot.target)
                t["start_utc"] = slot.start_utc.isoformat(timespec="seconds") + "Z"
                t["end_utc"] = slot.end_utc.isoformat(timespec="seconds") + "Z"
                t["scheduled_for_min"] = slot.dwell_min
                if slot.truncated_from_min:
                    t["scheduled_truncated_from_min"] = slot.truncated_from_min
                if slot.meridian_crossing_utc:
                    t["meridian_crossing_utc"] = (
                        slot.meridian_crossing_utc.isoformat(timespec="seconds") + "Z")
                    t["flip_required"] = slot.flip_required
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

        # Empty-plan advisory: if no visible targets, attach a clear
        # explanation but STILL publish the plan as READY. Per the
        # operator's principle ("I always want a session planned and
        # created no matter the warnings or no-gos"), the Planner
        # never cancels a session on its own — that's an explicit
        # operator action via the cancel_session tool only.
        empty_plan_advisory: dict | None = None
        if not full:
            constraint_note = ""
            if applied_constraints:
                constraint_note = (f" after applying {', '.join(applied_constraints)}"
                                     if applied_constraints else "")
            empty_reason = (f"No visible targets for tonight{constraint_note}. "
                              f"Active campaigns: {len(campaigns)}, "
                              f"skipped below horizon: {skipped_below_horizon}, "
                              f"skipped no coords: {skipped_no_coords}.")
            empty_plan_advisory = {
                "kind": "empty_plan",
                "severity": "warning",
                "message": empty_reason,
                "source": "planner",
            }
            self.log.info("rebuild_plan: empty target list — publishing "
                            "READY plan with empty_plan advisory")

        # Plan is READY the moment it's built. No multi-stage gated
        # approval pipeline — the operator wants the plan immediately,
        # with checks running asynchronously as advisories.
        try:
            from atlas.agents.session_workflow import (
                Advisory, SessionPlanState, new_review_id, STATE_READY,
            )
            from datetime import datetime as _dt
            top_names = [t["target_name"] for t in scheduled[:5]]
            review = SessionPlanState(
                review_id=new_review_id(),
                plan=plan,
                started_at=plan["built_at"],
                state=STATE_READY,
            )
            # Attach the empty-plan advisory inline so the dashboard
            # shows it with the very first READY publish (don't wait
            # for Critic/Oracle to add their own).
            if empty_plan_advisory is not None:
                review.add_advisory(Advisory(
                    kind=empty_plan_advisory["kind"],
                    severity=empty_plan_advisory["severity"],
                    message=empty_plan_advisory["message"],
                    source=empty_plan_advisory["source"],
                    at=_dt.utcnow().isoformat(timespec="seconds") + "Z",
                ))
            # Publish DRAFT immediately so the dashboard's Plan tab is
            # never empty. The review chain will produce a FINAL plan
            # that overwrites this draft when complete.
            #
            # Review chain (operator-specified, 2026-05-24):
            #   Planner DRAFT
            #     → Critic (weather/moon/hardware advisories)
            #     → Operator (verdict context, no-go notes)
            #     → Oracle (revisit + extended-integration suggestions)
            #     → Planner FINAL (incorporates all suggestions,
            #                       re-publishes the plan)
            # The Plan tab shows DRAFT while the chain is in flight,
            # then FINAL when the Planner finishes. Operator can
            # direct changes via chat at any time; the Planner
            # re-runs the chain from the start.
            get_state().set_session_review(review.to_jsonable())

            # ---- Plan-hash skip ------------------------------------
            # If the plan's material content is identical to the
            # previously published plan, the chain already produced
            # advisories that still apply. Skip the chain to save
            # ~192 inter-agent messages on quiet days where nothing
            # has actually changed (no new campaigns, no candidate
            # targets, no operator action). Forced rebuilds (operator
            # tool, revision_request) bypass this skip by always
            # passing a unique reason that differs from the cached
            # hash's reason — actually we just hash material fields,
            # so forced rebuilds that don't change the plan content
            # legitimately skip too (correct behaviour: nothing to
            # review).
            new_hash = self._plan_material_hash(plan)
            prev_hash = get_state().get_last_plan_hash()
            if new_hash == prev_hash and reason not in (
                    "startup", "revision_request", "candidate_target",
                    "operator_chat_request"):
                self.set_task(
                    f"plan unchanged ({len(scheduled)} target(s)); "
                    "skipping review chain — previous advisories still "
                    "apply. Plan tab updated with refreshed timestamp.",
                    state="idle",
                )
                get_state().set_review_phase("final", review_id=review.review_id)
                self.log.info("plan-hash unchanged (%s); chain skipped — "
                                "saved 4 bus messages + per-agent dispatch",
                                new_hash[:8])
                return
            get_state().set_last_plan_hash(new_hash)

            get_state().set_review_phase("critic", review_id=review.review_id)
            self.set_task(
                f"Stage 1/5: Planner published DRAFT ({len(scheduled)} "
                f"target(s)) — chain auto-starting to Critic",
                state="working",
            )
            try:
                await self.bus.broadcast_event({
                    "type": "review_chain_stage",
                    "sender": "planner",
                    "stage": "1/5",
                    "agent": "planner",
                    "phase": "draft",
                    "review_id": review.review_id,
                    "n_targets": len(scheduled),
                    "sent_at": plan["built_at"],
                })
            except Exception:
                pass
            await self.send(
                AgentName.CRITIC, AgentMessageKind.STATUS,
                payload={
                    "summary": (f"Plan DRAFT built ({reason}) — "
                                  f"{len(scheduled)} target(s). Top: "
                                  f"{', '.join(top_names) if top_names else '(none)'}. "
                                  "Begin review chain (Critic stage)."),
                    "kind": "plan_review",
                    "phase": "critic",
                    "review_id": review.review_id,
                    "review": review.to_jsonable(),
                    "from_chat": False,
                },
            )
        except Exception:
            self.log.exception("Failed to initiate review chain")
            try:
                get_state().set_review_phase("stalled")
            except Exception:
                pass

    @staticmethod
    def _plan_material_hash(plan: dict) -> str:
        """Compute a hash of the plan's *material* fields — the bits
        that, if unchanged, mean a fresh review chain would produce
        identical advisories. Excludes built_at, reason (those change
        on every rebuild but don't affect content)."""
        import hashlib as _hashlib
        import json as _json
        material = {
            "active_campaigns": plan.get("active_campaigns"),
            "considered_count": plan.get("considered_count"),
            "scheduled_total_min": plan.get("scheduled_total_min"),
            "dark_window_min": plan.get("dark_window_min"),
            "fallback_to_catalog": plan.get("fallback_to_catalog"),
            "applied_constraints": plan.get("applied_constraints"),
            "horizon_alt_min_deg": plan.get("horizon_alt_min_deg"),
            "visible_targets": [
                {"name": t.get("target_name"),
                 "ra": t.get("ra_deg"),
                 "dec": t.get("dec_deg"),
                 "workflow": t.get("workflow"),
                 "campaign": t.get("campaign_name"),
                 "scheduled_for_min": t.get("scheduled_for_min")}
                for t in (plan.get("visible_targets") or [])
            ],
        }
        blob = _json.dumps(material, sort_keys=True, default=str)
        return _hashlib.sha256(blob.encode("utf-8")).hexdigest()

    async def safe_mode_step(self) -> None:
        # Planner doesn't talk to Claude in this phase, so safe mode is a no-op.
        await asyncio.sleep(30)
