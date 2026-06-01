"""Operator agent — final authority on every autonomous decision.

The Operator processes inbound messages on its queue and decides what to do:
- Critic alerts: evaluate severity, choose response (continue / standby /
  shutdown / human-escalate)
- Planner output: review and push to NINA
- Oracle proposals: forward to Planner for scheduling
- Direct operator commands: execute immediately, overriding everything
"""
from __future__ import annotations

import asyncio

from datetime import datetime

from atlas.agents.base import BaseAgent
from atlas.agents.operator_tools import all_operator_tools
from atlas.agents.state import (
    OperatorVerdict, VERDICT_CAUTION, VERDICT_GO, VERDICT_NOGO,
    VERDICT_UNKNOWN, get_state,
)
from atlas.db.managers import AlertManager, SessionManager
from atlas.db.models import (
    AgentMessageKind, AgentName, AlertSeverity, SessionState,
)


# How long the verdict must stay GO/CAUTION after a NO-GO before we trust
# the clear and start the recovery sequence. Prevents flapping when wind
# oscillates around the critical threshold.
VERDICT_CLEAR_HYSTERESIS_S = 10 * 60   # 10 minutes

# How much astronomical-dark time we need to consider a restart worthwhile.
# Adds the (configurable) safe-startup overhead — 15 min in sim, closer to
# 20 min on real hardware where camera cooldown dominates — to the
# minimum useful imaging window.
MIN_USEFUL_IMAGING_MIN = 30.0
SAFE_STARTUP_OVERHEAD_MIN = 15.0


class Operator(BaseAgent):
    name = AgentName.OPERATOR

    def __init__(self) -> None:
        super().__init__()
        self._current_session_id: int | None = None
        self._auto_fix_attempts: dict[str, int] = {}  # code -> attempts
        # Verdict watcher state. The watcher fires shutdown on NO-GO,
        # startup-and-replan on the return to GO/CAUTION (after the
        # hysteresis window).
        self._verdict_block_at: datetime | None = None    # when NO-GO first hit
        self._verdict_clear_at: datetime | None = None    # when NO-GO ended
        self._verdict_last: str | None = None
        self._shutdown_in_progress = False
        self._startup_in_progress = False
        # Background tasks owned by the Operator
        self._verdict_watcher_task: asyncio.Task | None = None
        # Strong references to fire-and-forget background tasks. Without
        # this, asyncio only holds a weak reference and a task can be
        # garbage-collected mid-flight; its exceptions are also swallowed
        # until GC. We keep the ref until the task completes.
        self._bg_tasks: set[asyncio.Task] = set()
        # Confidence-layer consult dedup. The watcher re-runs the rule
        # chain on every verdict tick + the 300 s fallback. Without a
        # cooldown, a rule that keeps matching the same stale condition
        # (e.g. an expired active_slot the executor isn't updating, or a
        # dawn-truncate that can't apply) would spawn a fresh pending
        # decision every few minutes forever. We remember the last
        # acted-on recommendation signature + time and skip re-firing
        # the identical one within the cooldown window.
        self._last_consult_sig: str | None = None
        self._last_consult_at: float = 0.0
        # Register chat-time tools (weather, system status). Without these,
        # the dashboard's ATLAS-tab chat could only answer from training
        # knowledge — the Operator literally had no way to fetch live state.
        for spec in all_operator_tools():
            self.register_tool(spec)

    async def run(self) -> None:
        self.log.info("Operator agent online — final authority")
        self.set_task("standing by — final-authority watch on agent bus",
                      state="idle")
        # Verdict-transition watcher. Runs every 15 seconds; reads the
        # current OperatorVerdict from shared state and decides whether
        # to fire safe-shutdown (NO-GO) or safe-startup + replan (GO/
        # CAUTION after the hysteresis). This is the loop that turns
        # the verdict into real hardware action.
        self._verdict_watcher_task = asyncio.create_task(
            self._verdict_watcher_loop(),
            name="operator-verdict-watcher",
        )
        # Autonomous-session loop. Off by default; gated on the
        # auto_start_sessions DB flag. When enabled and all prereqs
        # align (verdict GO + astro_dark + plan READY + no session
        # + no manual + no cloudover-forecast advisory), starts a
        # session autonomously, runs SafeStartupSequence, walks
        # the plan's scheduled slots, then stops near dawn.
        self._auto_session_task: asyncio.Task | None = (
            asyncio.create_task(
                self._autonomous_session_loop(),
                name="operator-autonomous-session",
            )
        )
        self._auto_session_active = False
        self._auto_session_active_slot_idx: int = -1
        # Background task: run the comprehensive pre-flight every 2 min and
        # publish the aggregated verdict (weather + hardware + calibration
        # + plan + disk + vault + API + dark window) to shared state. The
        # dashboard's Session Readiness panel reads this; the verdict-on-
        # weather logic still fires immediately on Critic STATUS messages.
        preflight_task = asyncio.create_task(self._preflight_loop(),
                                               name="operator-preflight")
        try:
            # Event-driven main loop: block until a real message arrives.
            # No 5-second polling tick; nothing happens unless something
            # actually changed. The pre-flight loop above still ticks
            # periodically (much less often than before) to catch drift
            # — but a normal night sees zero idle wake-ups here.
            while not self.should_stop:
                try:
                    msg = await self.recv()
                except (asyncio.CancelledError, RuntimeError):
                    break
                try:
                    kind = msg.kind.value if hasattr(msg.kind, "value") else str(msg.kind)
                    sender = msg.sender.value if hasattr(msg.sender, "value") else str(msg.sender)
                    self.set_task(f"processing {kind} from {sender}", state="working")
                    await self._handle(msg)
                    self._mark_msg_handled(msg, ok=True)
                    self.set_task("standing by — last action handled", state="idle")
                except Exception as e:
                    self.log.exception("Operator failed handling message: %s", msg.kind)
                    self._mark_msg_handled(msg, ok=False,
                                              error=f"{type(e).__name__}: {e}")
                    self.set_task("error handling last message — see log",
                                  state="idle")
        finally:
            preflight_task.cancel()
            if self._verdict_watcher_task is not None:
                self._verdict_watcher_task.cancel()
            if self._auto_session_task is not None:
                self._auto_session_task.cancel()
            try:
                await preflight_task
            except (asyncio.CancelledError, Exception):
                pass
            if self._verdict_watcher_task is not None:
                try:
                    await self._verdict_watcher_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._auto_session_task is not None:
                try:
                    await self._auto_session_task
                except (asyncio.CancelledError, Exception):
                    pass

    # ---- Verdict watcher: convert verdict transitions into actions -----

    async def _consult_confidence_layer(self) -> None:
        """Run Confidence Layer 1 against the current Right Now snapshot
        and deliberate on any softer recommendation.

        Doctrine path: rules -> narrator.deliberate() -> adapt_plan().
        The deliberation appears in Pending Decisions; the human can
        override. On timeout the verb runs (or skips, per the rule's
        default).

        Conservative scope vs the legacy NO-GO transitions:
          - The legacy path owns hard NO-GO -> shutdown + clear-hysteresis
            recovery. We do NOT duplicate those here.
          - We act only on recommendations whose verb is one of the
            *softer* adaptations: drop_slot, truncate, swap, insert.
            (pause/resume/safe_shutdown are owned by the legacy code +
            hard-stop pre-empt.)
          - Skip if a deliberation is already live or a protection
            sequence is running — don't pile up.
        """
        try:
            from atlas.agents.confidence import recommend
            from atlas.agents.narrator import deliberate, active_decisions
        except Exception:
            return  # confidence module optional during import
        if self._shutdown_in_progress or self._startup_in_progress:
            return
        if get_state().is_manual():
            return
        if active_decisions():
            return  # one deliberation at a time keeps the dashboard sane
        rn = get_state().get_right_now()
        rec = recommend(rn)
        if rec is None or rec.verb in ("no_change", "pause", "resume",
                                          "safe_shutdown"):
            # Condition cleared (or owned by a legacy path) — reset the
            # cooldown so a genuinely new occurrence can fire immediately.
            self._last_consult_sig = None
            return  # legacy paths own these; nothing for us here

        # Cooldown dedup: skip if this exact recommendation was already
        # deliberated within the cooldown window. Signature keys on the
        # rule + verb + target so a *different* slot expiring still fires
        # right away, but the same one doesn't re-spawn endlessly.
        CONSULT_COOLDOWN_S = 900.0   # 15 min
        DECIDE_AFTER_S = 180.0
        target = (rec.verb_kwargs or {}).get("target_name") or ""
        sig = f"{rec.rule_name}:{rec.verb}:{target}"
        now_mono = asyncio.get_running_loop().time()
        if (sig == self._last_consult_sig
                and (now_mono - self._last_consult_at) < CONSULT_COOLDOWN_S):
            return
        self._last_consult_sig = sig
        self._last_consult_at = now_mono

        narration = (f"Considering: {rec.verb}. {rec.reason}. Watching "
                       f"for {int(DECIDE_AFTER_S)}s before acting. "
                       f"Operator override available.")
        # Fire-and-forget, but hold a strong ref so the task can't be
        # GC'd mid-deliberation and its exceptions surface.
        self._spawn_bg(
            deliberate(
                verb=rec.verb,
                reason=rec.reason,
                narration=narration,
                evidence=rec.evidence,
                decide_after_s=DECIDE_AFTER_S,
                default_action="apply",
                confidence_layer=rec.confidence_layer,
                severity=rec.severity,
                verb_kwargs=rec.verb_kwargs or {},
            ),
            name=f"deliberate-{rec.rule_name or rec.verb}",
        )
        self.log.info("Confidence layer 1 -> deliberate %s (rule=%s, reason=%s)",
                       rec.verb, rec.rule_name, rec.reason)

    def _spawn_bg(self, coro, *, name: str | None = None) -> "asyncio.Task":
        """Create a background task and hold a strong reference to it
        until it completes. asyncio only keeps a weak reference, so an
        un-referenced task can be garbage-collected mid-flight and its
        exceptions are swallowed. This keeps it alive + surfaces errors."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)

        def _done(t: "asyncio.Task") -> None:
            self._bg_tasks.discard(t)
            try:
                exc = t.exception()
            except (asyncio.CancelledError, Exception):
                exc = None
            if exc is not None:
                self.log.error("background task %s failed: %r",
                                 t.get_name(), exc)

        task.add_done_callback(_done)
        return task

    async def _verdict_watcher_loop(self) -> None:
        """Watch OperatorVerdict transitions, fire protection + recovery.

        Three transitions matter:

          GO/CAUTION → NO-GO
            Fire SafeShutdownSequence in a background task. Mark the
            block timestamp. Plan is left alone.

          NO-GO → GO/CAUTION
            Mark the clear timestamp. Don't act yet — we wait the full
            hysteresis window to confirm the clear is real (wind
            oscillating around threshold shouldn't restart/stop
            repeatedly).

          NO-GO → GO/CAUTION sustained for VERDICT_CLEAR_HYSTERESIS_S
            Fire the worthwhile-restart check + SafeStartupSequence +
            REVISION_REQUEST so the Planner rebuilds for the remaining
            dark window.
        """
        await asyncio.sleep(2)
        while not self.should_stop:
            try:
                await self._tick_verdict_watcher()
            except Exception:
                self.log.exception("verdict watcher tick failed")
            # Event-driven wait: state.set_verdict() fires an
            # asyncio.Event the moment a transition happens, so this
            # loop wakes within milliseconds of a real change. The
            # 300-s timeout is a safety fallback (clock-time effects
            # like "dark window opened" or "hysteresis window expired"
            # need the loop to wake up even without a verdict change).
            # Replaces the old 15-s polling cadence — ~5,500 fewer
            # wake-ups per day with identical responsiveness on real
            # verdict transitions.
            try:
                await get_state().wait_verdict_change(timeout_s=300.0)
            except Exception:
                # If wait_verdict_change itself fails, fall back to
                # the legacy short sleep so the watcher never wedges.
                await asyncio.sleep(15)

    async def _tick_verdict_watcher(self) -> None:
        # Manual override pauses all autonomous recovery
        if get_state().is_manual():
            return
        v = get_state().get_verdict()
        if v is None:
            return
        prev = self._verdict_last
        self._verdict_last = v.verdict

        # Transition into NO-GO
        if v.verdict == VERDICT_NOGO and prev not in (VERDICT_NOGO, None):
            self._verdict_block_at = datetime.utcnow()
            self._verdict_clear_at = None
            self.log.warning(
                "Verdict %s → NO-GO; firing SafeShutdownSequence (reason: %s)",
                prev, v.reason,
            )
            if not self._shutdown_in_progress:
                asyncio.create_task(
                    self._run_shutdown(reason=v.reason or "no-go verdict"),
                    name="operator-shutdown",
                )

        # Transition out of NO-GO — start counting toward hysteresis
        if v.verdict != VERDICT_NOGO and prev == VERDICT_NOGO:
            self._verdict_clear_at = datetime.utcnow()
            self.log.info(
                "Verdict NO-GO → %s; clear timer started (%.0fs hysteresis).",
                v.verdict, VERDICT_CLEAR_HYSTERESIS_S,
            )

        # Sustained clear → maybe recover
        if (v.verdict != VERDICT_NOGO and self._verdict_clear_at is not None):
            elapsed = (datetime.utcnow() - self._verdict_clear_at).total_seconds()
            if elapsed >= VERDICT_CLEAR_HYSTERESIS_S:
                # Hysteresis satisfied — consider recovery once.
                self._verdict_clear_at = None
                self._verdict_block_at = None
                if not self._startup_in_progress:
                    asyncio.create_task(
                        self._maybe_recover(verdict_reason=v.reason or ""),
                        name="operator-recovery",
                    )

        # After the legacy NO-GO transitions, consult Confidence Layer 1
        # for softer adaptations (drop expired slot, truncate near dawn,
        # etc.). Doctrine: rules -> narrate -> adapt. Override available.
        await self._consult_confidence_layer()

    async def _run_shutdown(self, *, reason: str) -> None:
        """Run SafeShutdownSequence end-to-end. Captures progress events
        + the terminal summary into the operator lane / bus broadcasts."""
        from atlas.config import is_simulation_mode
        from atlas.db.managers import ConfigManager
        from atlas.safety.protection import SafeShutdownSequence
        self._shutdown_in_progress = True
        try:
            sim = is_simulation_mode()
            equip = ConfigManager.get_equipment()
            if sim or equip is None:
                from atlas.simulation.fake_hardware import FakeNina, FakePhd2
                nina, phd2 = FakeNina(), FakePhd2()
            else:
                from atlas.hardware.nina import NinaClient
                from atlas.hardware.phd2 import Phd2Client
                nina = NinaClient(host=equip.nina_host, port=equip.nina_port,
                                    timeout=15.0)
                phd2 = Phd2Client(host=equip.phd2_host, port=equip.phd2_port,
                                    timeout=10.0)
            close_roof = bool(equip and getattr(equip, "roof_mode", "") == "nina")
            seq = SafeShutdownSequence(
                nina=nina, phd2=phd2, reason=reason,
                close_roof=close_roof,
                active_capture=getattr(self, "_current_capture", None),
            )
            async for ev in seq.run():
                try:
                    await self.bus.broadcast_event({
                        "type": "protection",
                        "sender": "operator",
                        "direction": "shutdown",
                        **ev,
                        "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    })
                except Exception:
                    pass
                self.set_task(f"shutdown: {ev.get('summary','')[:80]}",
                              state="working")
            final = seq.result or {"state": "unknown"}
            self.log_decision(
                "safe_shutdown_complete",
                inputs={"reason": reason},
                outputs=final,
                rationale=final.get("summary", ""),
                session_id=self._current_session_id,
            )
            if self._current_session_id is not None:
                try:
                    SessionManager.set_state(
                        self._current_session_id, SessionState.STANDBY,
                        reason=f"safe-shutdown: {reason}",
                    )
                except Exception:
                    self.log.exception("Could not STANDBY active session")
            self.set_task(
                f"shutdown {final.get('state')} — awaiting clear",
                state="waiting",
            )
        finally:
            self._shutdown_in_progress = False
            try:
                await nina.close()
            except Exception:
                pass
            try:
                await phd2.close()
            except Exception:
                pass

    async def _maybe_recover(self, *, verdict_reason: str) -> None:
        """Decide whether to fire SafeStartupSequence + replan, or
        log that the remaining window isn't worth restarting for."""
        from atlas.db.managers import ConfigManager
        site = ConfigManager.get_site()
        if site is None:
            self.log.info("Recovery skipped: no site configured.")
            return
        from atlas.astronomy.day_phase import (
            current_phase, minutes_useful_remaining,
        )
        phase = current_phase(float(site.latitude), float(site.longitude))

        # If we're outside astronomical dark, no restart needed —
        # nothing was being imaged. The verdict clear matters but
        # there's nothing to recover from.
        if not phase.is_imaging_window:
            self.log.info(
                "Verdict cleared, but we're in %s (sun %.1f°). "
                "No recovery needed; next dark window in ~%.0f min.",
                phase.phase, phase.sun_altitude_deg,
                phase.minutes_until_next_phase,
            )
            self.log_decision(
                "recovery_not_needed_outside_dark",
                inputs={"phase": phase.phase,
                          "minutes_to_dark": phase.minutes_until_next_phase},
                rationale="Verdict clear but not currently imaging.",
            )
            return

        usable = minutes_useful_remaining(
            phase, safe_startup_overhead_min=SAFE_STARTUP_OVERHEAD_MIN,
        ) or 0.0
        if usable < MIN_USEFUL_IMAGING_MIN:
            self.log.warning(
                "Verdict cleared with only %.0f usable min left "
                "(< %.0f min minimum); skipping restart. "
                "Plan stays READY; next dark window resumes normally.",
                usable, MIN_USEFUL_IMAGING_MIN,
            )
            self.set_task(
                f"verdict clear but only {usable:.0f} min usable — "
                "not worth restarting tonight",
                state="idle",
            )
            self.log_decision(
                "recovery_skipped_insufficient_time",
                inputs={"usable_minutes": usable,
                          "minimum_useful_minutes": MIN_USEFUL_IMAGING_MIN},
                rationale=("Restart overhead + minimum useful imaging "
                             "exceeds remaining astronomical dark."),
            )
            return

        # Worth restarting. Fire SafeStartupSequence then nudge the
        # Planner to rebuild for the remaining window.
        self.log.info(
            "Verdict cleared with %.0f usable min remaining (≥ %.0f min). "
            "Firing SafeStartupSequence + Planner replan.",
            usable, MIN_USEFUL_IMAGING_MIN,
        )
        await self._run_startup(verdict_reason=verdict_reason)
        # Replan for the remaining window. The Planner is clock-aware:
        # _rebuild_plan(reason="conditions_cleared") will compute a
        # plan that fits between now and dawn.
        try:
            await self.send(
                AgentName.PLANNER,
                AgentMessageKind.REVISION_REQUEST,
                payload={"reason": "conditions_cleared_mid_night",
                          "verdict_reason": verdict_reason,
                          "usable_minutes_remaining": usable},
            )
        except Exception:
            self.log.exception("Failed to send replan request to Planner")

    async def _run_startup(self, *, verdict_reason: str) -> None:
        """Run SafeStartupSequence and broadcast progress."""
        from atlas.config import is_simulation_mode
        from atlas.db.managers import ConfigManager
        from atlas.safety.protection import SafeStartupSequence
        self._startup_in_progress = True
        try:
            sim = is_simulation_mode()
            equip = ConfigManager.get_equipment()
            if sim or equip is None:
                from atlas.simulation.fake_hardware import FakeNina, FakePhd2
                nina, phd2 = FakeNina(), FakePhd2()
                setpoint = -10.0
            else:
                from atlas.hardware.nina import NinaClient
                from atlas.hardware.phd2 import Phd2Client
                nina = NinaClient(host=equip.nina_host, port=equip.nina_port,
                                    timeout=15.0)
                phd2 = Phd2Client(host=equip.phd2_host, port=equip.phd2_port,
                                    timeout=10.0)
                setpoint = getattr(equip, "cooling_setpoint_c", None)
            seq = SafeStartupSequence(
                nina=nina, phd2=phd2,
                cooling_setpoint_c=setpoint, simulation=sim,
            )
            async for ev in seq.run():
                try:
                    await self.bus.broadcast_event({
                        "type": "protection",
                        "sender": "operator",
                        "direction": "startup",
                        **ev,
                        "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    })
                except Exception:
                    pass
                self.set_task(f"startup: {ev.get('summary','')[:80]}",
                              state="working")
            final = seq.result or {"state": "unknown"}
            self.log_decision(
                "safe_startup_complete",
                inputs={"verdict_reason": verdict_reason},
                outputs=final,
                rationale=final.get("summary", ""),
                session_id=self._current_session_id,
            )
            self.set_task(f"startup {final.get('state')} — ready to resume",
                          state="idle")
        finally:
            self._startup_in_progress = False
            try:
                await nina.close()
            except Exception:
                pass
            try:
                await phd2.close()
            except Exception:
                pass

    # ---- Autonomous session loop -------------------------------------------

    async def _autonomous_session_loop(self) -> None:
        """Watch for the moment all preconditions align, then run an
        autonomous session: SafeStartupSequence → walk plan slots →
        stop at dawn → SafeShutdownSequence.

        Off by default; activated only when the operator flips
        auto_start_sessions on the Setup tab. Even when on, ALL of
        these must be true at the same time for autonomy to kick in:

          * day_phase = ASTRO_DARK and minutes_until_dawn >= MIN_USEFUL+OVERHEAD
          * verdict = GO  (CAUTION won't trigger — operator's call only)
          * plan READY with at least one scheduled slot
          * no active session
          * manual control NOT engaged
          * no critical advisories of kind=session_wasted_forecast
            or kind=hardware on the live plan

        Ticks every 30 s — slow because every condition is read from
        in-memory shared state and a single tick is essentially free.
        The actual session walks proceeds in a separate task once
        kicked off."""
        from atlas.db.managers import ConfigManager
        from atlas.db.models import SessionState as _SS
        await asyncio.sleep(20)   # let startup settle
        while not self.should_stop:
            try:
                await self._autonomous_session_tick()
            except Exception:
                self.log.exception("autonomous-session tick failed")
            # Adaptive cadence: when verdict is GO (we're potentially
            # about to fire), tick every 30 s for responsiveness. When
            # verdict is NO-GO/CAUTION/UNKNOWN/missing (nothing to fire
            # anyway), tick every 5 min. Saves ~2,300 wake-ups/day
            # during daytime + storm hours when conditions clearly
            # don't allow autonomous start.
            try:
                v = get_state().get_verdict()
                if v is not None and v.verdict == VERDICT_GO:
                    await asyncio.sleep(30)
                else:
                    await asyncio.sleep(300)
            except Exception:
                await asyncio.sleep(30)

    async def _autonomous_session_tick(self) -> None:
        from atlas.db.managers import ConfigManager
        # Honor the toggle. If off, do nothing.
        flags = ConfigManager.get_system_flags()
        if not bool(getattr(flags, "auto_start_sessions", False)):
            return
        # Already running a session OR mid-execution → skip
        if self._auto_session_active:
            return
        if self._current_session_id is not None:
            return
        if get_state().is_manual():
            return
        # Verdict must be GO (not CAUTION — too marginal for autonomy)
        v = get_state().get_verdict()
        if v is None or v.verdict != VERDICT_GO:
            return
        # Plan must be READY with slots
        review = get_state().get_session_review() or {}
        if review.get("state") != "ready":
            return
        slots = (review.get("plan") or {}).get("visible_targets") or []
        if not slots:
            return
        # Critical advisories that block autonomous start
        for a in review.get("advisories") or []:
            if a.get("severity") == "critical" and a.get("kind") in (
                "session_wasted_forecast", "hardware",
            ):
                self.log.info(
                    "auto-start blocked: critical advisory %s — %s",
                    a.get("kind"), a.get("message", "")[:80],
                )
                return
        # Must currently be in astronomical dark with enough usable time
        site = ConfigManager.get_site()
        if site is None:
            return
        from atlas.astronomy.day_phase import (
            current_phase, minutes_useful_remaining,
        )
        phase = current_phase(float(site.latitude), float(site.longitude))
        if not phase.is_imaging_window:
            return
        usable = minutes_useful_remaining(
            phase, safe_startup_overhead_min=SAFE_STARTUP_OVERHEAD_MIN,
        ) or 0.0
        if usable < MIN_USEFUL_IMAGING_MIN:
            self.log.info(
                "auto-start declined: only %.0f usable min remaining",
                usable,
            )
            return

        # All preconditions met — fire the session.
        self._auto_session_active = True
        self.log.warning(
            "AUTO-START: conditions aligned (verdict=GO, dark with "
            "%.0f usable min, plan READY with %d slot(s))",
            usable, len(slots),
        )
        self.log_decision(
            "auto_session_start_triggered",
            inputs={"usable_min": usable, "slots": len(slots),
                      "review_id": review.get("review_id")},
            rationale="all auto-start preconditions met",
        )
        asyncio.create_task(
            self._run_autonomous_session(review),
            name="operator-auto-session-run",
        )

    async def _run_autonomous_session(self, review: dict) -> None:
        """One full autonomous night: startup → slots → shutdown."""
        from atlas.db.managers import ConfigManager
        from atlas.db.models import SessionState as _SS
        try:
            # 1. SafeStartupSequence (cool camera, unpark mount, etc.)
            await self._run_startup(verdict_reason="auto-start")

            # 2. Create the session row
            from atlas.config import is_simulation_mode
            sim = is_simulation_mode()
            sid = SessionManager.start(simulation=sim)
            SessionManager.set_state(sid, SessionState.NOMINAL,
                                       reason="autonomous start")
            self._current_session_id = sid
            self.log.warning("AUTO-START: session #%d started", sid)
            await self.bus.broadcast_event({
                "type": "session_started",
                "sender": "operator",
                "session_id": sid, "simulation": sim,
                "autonomous": True,
                "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            })
            # Session started: Archivist needs to know so it can prep
            # the watch-folder + start filtering ingest. Other agents
            # see this via the dashboard's bus broadcast — no need to
            # send copies.
            await self._notify_agents_of_decision(
                kind="session_started",
                summary=f"Autonomous session #{sid} started ({'sim' if sim else 'live'})",
                to=(AgentName.ARCHIVIST,),
                session_id=sid, simulation=sim, autonomous=True,
            )

            # 3. Walk the plan's scheduled slots in time order
            await self._walk_plan_slots(review, sid)

            # 4. Stop the session cleanly
            SessionManager.set_state(sid, SessionState.COMPLETE,
                                       reason="autonomous stop at dawn")
            await self.send(
                AgentName.ARCHIVIST,
                AgentMessageKind.POST_SESSION,
                payload={"session_id": sid,
                          "summary": f"Autonomous session #{sid} ended"},
                session_id=sid,
            )
            self.log.warning("AUTO-START: session #%d complete", sid)
            await self.bus.broadcast_event({
                "type": "session_stopped",
                "sender": "operator",
                "session_id": sid, "autonomous": True,
                "reason": "dawn",
                "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            })
            # Session stopped: Archivist already gets a POST_SESSION
            # message (separately, above), and the dashboard sees the
            # bus broadcast. No additional sibling relay needed.
            await self._notify_agents_of_decision(
                kind="session_stopped",
                summary=f"Autonomous session #{sid} stopped at dawn",
                to=(),
                session_id=sid, autonomous=True, reason="dawn",
            )
            self._current_session_id = None

            # 5. SafeShutdownSequence (park mount, warm camera, close roof)
            await self._run_shutdown(reason="auto-stop at dawn")

        except Exception:
            self.log.exception("autonomous session run failed")
        finally:
            self._auto_session_active = False
            self._auto_session_active_slot_idx = -1

    async def _walk_plan_slots(self, review: dict, session_id: int) -> None:
        """Fire start_capture_sequence for each scheduled slot in order.
        Sleeps to the slot's start_utc before firing. Exits early when:
          - the verdict goes NO-GO (verdict watcher already fired
            shutdown; we just stop the walk)
          - we leave the astronomical dark window
          - all slots are processed

        Populates the execution snapshot at each transition so the
        Right Now view's Procedural layer reflects what's actually
        running:
          active_slot           - the current slot dict
          active_action         - human-readable phase string
          next_action / next_action_at - the upcoming transition
          planned_session_end   - last slot's end_utc, set once at start
        On exit (success or early), clears the execution snapshot so
        the dashboard returns to "no slot active" cleanly.
        """
        from atlas.db.managers import ConfigManager
        from datetime import datetime as _dt
        slots = (review.get("plan") or {}).get("visible_targets") or []
        site = ConfigManager.get_site()
        if site is None:
            return
        # Set planned_session_end from the last slot once at start.
        if slots:
            last_end = slots[-1].get("end_utc")
            if last_end:
                get_state().update_execution(planned_session_end=last_end)
        # The actual walk is done by a private coroutine wrapped in a
        # try/finally below so every early-return path (NO-GO, dark
        # window over, stop signal, exception) still clears the
        # execution snapshot.
        try:
            await self._walk_plan_slots_inner(slots, session_id)
        finally:
            get_state().clear_execution()

    async def _walk_plan_slots_inner(self, slots: list, session_id: int) -> None:
        from datetime import datetime as _dt
        for idx, slot in enumerate(slots):
            if self.should_stop:
                return
            # Hard exits — verdict turned bad, dark window over
            v = get_state().get_verdict()
            if v is None or v.verdict == VERDICT_NOGO:
                self.log.warning(
                    "auto-session: verdict %s at slot %d/%d; abandoning walk",
                    v.verdict if v else "?", idx + 1, len(slots),
                )
                return
            from atlas.astronomy.day_phase import current_phase
            phase = current_phase(float(site.latitude),
                                    float(site.longitude))
            if not phase.is_imaging_window:
                self.log.warning(
                    "auto-session: left astronomical dark "
                    "(phase=%s); ending walk at slot %d/%d",
                    phase.phase, idx + 1, len(slots),
                )
                return

            # Sleep until slot.start_utc if it's in the future
            start_iso = slot.get("start_utc")
            if start_iso:
                try:
                    slot_start = _dt.fromisoformat(start_iso.rstrip("Z"))
                    wait_s = (slot_start - _dt.utcnow()).total_seconds()
                    if wait_s > 1.0:
                        # Populate execution snapshot with the next-action
                        # signal so Right Now's Procedural layer shows
                        # "Next: slew to <target> @ <ISO>" while we wait.
                        get_state().update_execution(
                            next_action=(
                                f"start slot {idx+1}/{len(slots)}: "
                                f"{slot.get('target_name') or '?'}"
                            ),
                            next_action_at=start_iso,
                        )
                        self.set_task(
                            f"auto-session slot {idx+1}/{len(slots)} "
                            f"({slot.get('target_name')}) starts in "
                            f"{int(wait_s)}s",
                            state="waiting",
                        )
                        # Sleep in 30s chunks so we can re-check verdict
                        # mid-wait without going dark for hours
                        remaining = wait_s
                        while remaining > 0 and not self.should_stop:
                            await asyncio.sleep(min(30.0, remaining))
                            remaining -= 30.0
                            v = get_state().get_verdict()
                            if v is None or v.verdict == VERDICT_NOGO:
                                self.log.warning(
                                    "auto-session: verdict went NO-GO "
                                    "while waiting for slot %d", idx + 1)
                                return
                except Exception:
                    pass
            self._auto_session_active_slot_idx = idx

            # Mark the slot as active in the Right Now Procedural layer.
            # Includes target_name + workflow + start/end window so the
            # dashboard shows "Slot: M51 (deepsky)" + the slot progress.
            next_slot = slots[idx + 1] if idx + 1 < len(slots) else None
            get_state().update_execution(
                active_slot={
                    "target_name": slot.get("target_name"),
                    "workflow": slot.get("workflow"),
                    "start_utc": slot.get("start_utc"),
                    "end_utc": slot.get("end_utc"),
                    "ra_deg": slot.get("ra_deg"),
                    "dec_deg": slot.get("dec_deg"),
                    "priority": slot.get("priority"),
                },
                active_action=f"dispatching capture sequence ({idx+1}/{len(slots)})",
                next_action=(
                    f"slot {idx+2}/{len(slots)}: {next_slot.get('target_name') or '?'}"
                    if next_slot else "end of session"
                ),
                next_action_at=(next_slot or {}).get("start_utc"),
                blocked_reason=None,
            )

            # Fire start_capture_sequence for this slot
            self.set_task(
                f"auto-session: capturing slot {idx+1}/{len(slots)} "
                f"({slot.get('target_name')})",
                state="working",
            )
            self.log.info(
                "auto-session: dispatching slot %d/%d %s",
                idx + 1, len(slots), slot.get("target_name"),
            )
            try:
                await self._cmd_start_capture_sequence({
                    "target": slot,
                    "exposure_plan": slot.get("exposure_plan"),
                    "dither_every_n_frames": 1,
                })
                get_state().update_execution(
                    active_action=f"capturing ({idx+1}/{len(slots)}) {slot.get('target_name') or '?'}",
                )
            except Exception:
                self.log.exception(
                    "auto-session: slot %d dispatch failed", idx + 1)
                get_state().update_execution(
                    active_action=f"slot {idx+1} dispatch failed; continuing",
                )
                continue

            # Wait until the slot's end_utc OR the capture completes
            # (whichever first). The capture sequence is in another
            # background task; we poll for end_utc here.
            end_iso = slot.get("end_utc")
            if end_iso:
                try:
                    slot_end = _dt.fromisoformat(end_iso.rstrip("Z"))
                    while _dt.utcnow() < slot_end and not self.should_stop:
                        await asyncio.sleep(30)
                        v = get_state().get_verdict()
                        if v is None or v.verdict == VERDICT_NOGO:
                            self.log.warning(
                                "auto-session: verdict went NO-GO "
                                "during slot %d capture", idx + 1)
                            return
                except Exception:
                    pass
        self.log.info("auto-session: walked all %d slot(s) to completion",
                      len(slots))
        # Note: the caller's try/finally clears the execution snapshot
        # so we don't need to do it here.

    async def _preflight_loop(self) -> None:
        """Periodic pre-flight tick.

        In the event-driven world this loop is a safety net, not the
        main mechanism — the API endpoints that mutate gate inputs
        (vault unlock, sim toggle, weather refresh) already call
        _refresh_preflight_now() so the dashboard updates instantly.
        This loop just catches the cases nothing else covers (calendar
        time crossing into dark window, a Claude API outage healing
        itself, etc.), so it runs much less often than the old
        2-minute cadence — 5 minutes is plenty.
        """
        from atlas.safety.preflight import run_session_preflight
        INTERVAL_S = 300   # was 120; mostly drift detection now
        last_verdict: str | None = None
        # Fire an immediate first pass so the dashboard has data on load.
        await asyncio.sleep(2)
        while not self.should_stop:
            try:
                preflight = await run_session_preflight()
                pf_dict = preflight.to_jsonable()
                get_state().set_preflight(pf_dict)
                # If the verdict has changed, also update OperatorVerdict
                # (which is what the legacy banner reads) and broadcast.
                if preflight.verdict != last_verdict:
                    self.log.info("Pre-flight verdict: %s -> %s (%s)",
                                    last_verdict, preflight.verdict,
                                    preflight.reason)
                    last_verdict = preflight.verdict
                    new_verdict = OperatorVerdict(
                        decided_at=preflight.assessed_at,
                        verdict=preflight.verdict,
                        reason=preflight.reason,
                        sources=["session_preflight"],
                    )
                    get_state().set_verdict(new_verdict)
                    self.log_decision(
                        "preflight_verdict",
                        inputs={"gates": [g.to_jsonable() for g in preflight.gates]},
                        outputs={"verdict": preflight.verdict,
                                  "reason": preflight.reason,
                                  "next_action": preflight.next_action},
                        rationale=preflight.reason,
                    )
                    try:
                        await self.bus.broadcast_event({
                            "type": "session_preflight",
                            "sender": "operator",
                            "kind": "preflight_verdict",
                            "verdict": preflight.verdict,
                            "reason": preflight.reason,
                            "next_action": preflight.next_action,
                            "sent_at": preflight.assessed_at,
                        })
                    except Exception:
                        pass
            except Exception:
                self.log.exception("Preflight loop failed")
            await asyncio.sleep(INTERVAL_S)

    async def _handle(self, msg) -> None:
        if msg.kind == AgentMessageKind.ALERT:
            await self._handle_alert(msg)
        elif msg.kind == AgentMessageKind.STATUS:
            await self._handle_status(msg)
        elif msg.kind == AgentMessageKind.REVISION_REQUEST:
            await self._forward_to_planner(msg)
        elif msg.kind == AgentMessageKind.CANDIDATE_TARGET:
            await self._forward_to_planner(msg)
        elif msg.kind == AgentMessageKind.OPERATOR_COMMAND:
            await self._handle_human_command(msg)
        else:
            # Unknown kind — surface to dashboard via the default relay
            # handler so chat-initiated hand-offs are visible at minimum.
            await self.handle_relayed_message(msg)

    async def _handle_status(self, msg) -> None:
        """Status updates from other agents:

          kind=weather_assessment   → fold into the GO/CAUTION/NO-GO verdict
          kind=critical_advisories  → evaluate whether to block execution
                                       (does NOT touch the plan itself)
          kind=plan_review(operator) → review chain stage 3: append
                                         Operator-context advisories then
                                         forward to Oracle (stage 4)
          (everything else)         → debug-log + ignore (advisories show up
                                       on the dashboard via shared state, not
                                       through the Operator's queue anymore)
        """
        payload = msg.payload or {}
        kind = payload.get("kind")
        if kind == "weather_assessment":
            await self._update_verdict_from_weather(payload)
            return
        if kind == "critical_advisories" and payload.get("advisories"):
            await self._evaluate_execution_block(payload)
            return
        if (kind == "plan_review"
              and payload.get("phase") == "operator"
              and payload.get("review")):
            await self._review_chain_operator_stage(payload)
            return
        self.log.debug("Operator ignoring status kind=%s", kind)

    async def _review_chain_operator_stage(self, payload: dict) -> None:
        """Stage 3 of the Planner→Critic→Operator→Oracle→Planner chain.

        Runs automatically — the Operator does NOT need to be asked.
        On receiving the plan_review STATUS, the Operator reads the
        plan out of the payload, runs its verification (target
        summary, dark-window fit, execution-gate state) and forwards
        to Oracle.

        Verification covers:
          * Target list summary + counts
          * Dark-window fit (X/Y min scheduled / dark)
          * Current verdict + manual-flag state (execution context)
        """
        # Visible stage broadcast so the dashboard's message-flow
        # shows "Stage 3: Operator auto-reviewing plan…" landing on
        # the bus before the work happens.
        try:
            plan_preview = (payload.get("review") or {}).get("plan") or {}
            n = len(plan_preview.get("visible_targets") or [])
            self.set_task(
                f"Stage 3/5: Operator auto-reviewing plan "
                f"({n} target(s)) — fit + execution context",
                state="working",
            )
            await self.bus.broadcast_event({
                "type": "review_chain_stage",
                "sender": "operator",
                "stage": "3/5",
                "agent": "operator",
                "phase": "operator",
                "review_id": payload.get("review_id"),
                "n_targets": n,
                "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            })
        except Exception:
            pass
        from atlas.agents.state import (
            VERDICT_GO, get_state,
        )
        from datetime import datetime as _dt
        review_id = payload.get("review_id") or ""
        review = payload.get("review") or {}
        plan = review.get("plan") or {}
        targets = plan.get("visible_targets") or []
        target_summary = (
            ", ".join(t.get("target_name", "?") for t in targets[:6])
            + (f" (+{len(targets) - 6} more)" if len(targets) > 6 else "")
        ) if targets else "(no scheduled targets)"
        scheduled_min = plan.get("scheduled_total_min")
        dark_min = plan.get("dark_window_min")
        fit_note = (f"{scheduled_min:.0f}/{dark_min:.0f} min dark filled"
                      if (scheduled_min is not None and dark_min)
                      else "fit unknown")

        # Operator's execution-context note
        verdict = get_state().get_verdict()
        manual = get_state().is_manual()
        gates: list[str] = []
        if verdict is not None and verdict.verdict != VERDICT_GO:
            gates.append(f"verdict {verdict.verdict}: {verdict.reason}")
        if manual:
            gates.append("manual control engaged")
        gate_note = ("execution gates: " + "; ".join(gates)
                        if gates else "no execution gates active")

        op_advisory = {
            "kind": "operator_review",
            "severity": "info",
            "message": (f"Operator reviewed plan: {len(targets)} target(s) "
                          f"scheduled ({target_summary}); {fit_note}. "
                          f"{gate_note}. Plan creation proceeds independent "
                          f"of execution gates — plan can be used tonight if "
                          f"conditions clear, or carried to a future night."),
            "source": "operator",
            "at": _dt.utcnow().isoformat(timespec="seconds") + "Z",
        }
        try:
            get_state().append_advisories(review_id, [op_advisory])
        except Exception:
            self.log.exception("Operator advisory append failed; "
                                  "continuing chain")
        # Forward to Oracle (stage 4)
        try:
            get_state().set_review_phase("oracle", review_id=review_id)
            await self.send(
                AgentName.ORACLE, AgentMessageKind.STATUS,
                payload={
                    "summary": ("Operator review-chain stage complete — "
                                  "forwarding to Oracle for revisit + "
                                  "extended-integration suggestions."),
                    "kind": "plan_review",
                    "phase": "oracle",
                    "review_id": review_id,
                    "review": payload.get("review"),
                },
            )
        except Exception:
            self.log.exception("Failed to forward review chain to Oracle stage")
            try:
                get_state().set_review_phase("stalled", review_id=review_id)
            except Exception:
                pass

    async def _evaluate_execution_block(self, payload: dict) -> None:
        """Decide whether incoming critical advisories warrant blocking
        *execution* — opening the roof, slewing the mount, running a
        sequence.

        Critical to keep straight: this never touches the plan. The
        plan is READY; it stays READY. What changes is the
        OperatorVerdict (GO / CAUTION / NO-GO), which is the gate
        elsewhere in the codebase that authorizes a session start.

        Execution-block triggers:

          * precipitation > 0 in current weather  (storm — roof stays closed)
          * wind > critical threshold             (mount/scope at risk)
          * any hardware kind=critical advisory   (mount/camera fault)

        Operator-warning advisories (humidity high, moon proximity,
        dew margin tight) are NOT blocks — the operator decides on
        those via the dashboard banner. When weather clears, the
        verdict flips back to GO/CAUTION and execution can resume
        against the same plan, no rebuild required.
        """
        if get_state().is_manual():
            self.log.info("Manual control engaged — skipping autonomous "
                            "execution-block evaluation.")
            self.log_decision("execution_block_paused_manual",
                                rationale="Operator is driving; not gating.")
            return

        advisories = payload.get("advisories") or []
        hardware_fault = any(a.get("kind") == "hardware" for a in advisories)
        disk_critical = any(a.get("kind") == "disk" for a in advisories)
        weather_storm = False
        a = get_state().get_assessment()
        if a is not None:
            raw = a.raw_current or {}
            precip = raw.get("precip_in") or 0.0
            wind_mph = raw.get("wind_speed_mph") or 0.0
            from atlas.safety.thresholds import SafetyThresholds
            t = SafetyThresholds.from_db()
            from atlas.units import ms_to_mph
            crit_mph = ms_to_mph(t.wind_speed_critical_ms)
            if precip > 0:
                weather_storm = True
            if wind_mph >= crit_mph:
                weather_storm = True

        if not (hardware_fault or weather_storm or disk_critical):
            self.log.info("Critical advisories filed but no execution-"
                            "block conditions met — verdict unchanged.")
            self.log_decision("execution_block_evaluated",
                                inputs={"advisories": advisories},
                                outputs={"blocked": False},
                                rationale="No storm / damage-risk / disk indicators")
            return

        # Execution block fires. Flip the verdict to NO-GO. The
        # _verdict_watcher_loop will detect this transition and
        # autonomously fire SafeShutdownSequence + park the session.
        # The plan itself stays READY — operator can still review
        # what was planned, and when conditions clear the watcher
        # handles startup + replan against the same plan.
        reasons = []
        if hardware_fault:
            reasons.append("hardware critical")
        if weather_storm:
            reasons.append("storm / extreme wind")
        if disk_critical:
            reasons.append("disk space critical")
        reason = " + ".join(reasons)
        new = OperatorVerdict(
            decided_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            verdict=VERDICT_NOGO, reason=f"Execution blocked: {reason}",
            sources=["execution_block", "critical_advisories"],
        )
        get_state().set_verdict(new)
        self.log.warning("EXECUTION BLOCK: %s (plan stays READY; "
                          "watcher will run shutdown sequence)", reason)
        self.log_decision("execution_block",
                            inputs={"advisories": advisories,
                                      "reason": reason},
                            rationale=reason)
        await self.bus.broadcast_event({
            "type": "execution_block",
            "sender": "operator",
            "reason": reason,
            "verdict": VERDICT_NOGO,
            "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })
        # Execution block is an Operator-internal decision — the dashboard
        # already sees it via the bus broadcast above. No sibling agent
        # needs a STATUS copy: the Planner KEEPS PLANNING (plan creation
        # is independent of execution), Critic is the SOURCE of the
        # block, Oracle keeps researching, Archivist has nothing to do
        # until frames land. Sending to=() means bus-event only, zero
        # relay noise.
        await self._notify_agents_of_decision(
            kind="execution_block",
            summary=f"Execution blocked: {reason}",
            to=(),
            reason=reason, verdict=VERDICT_NOGO, advisories=advisories,
        )
        # Page the operator via every configured notification channel.
        # Critical events always notify — that's the whole point of
        # the notification system.
        try:
            from atlas.notifications import Notification, get_dispatcher
            await get_dispatcher().dispatch(Notification(
                severity="critical",
                title="ATLAS: execution blocked",
                message=reason,
                detail=("Plan stays READY; shutdown sequence is running. "
                          "Verdict will return to GO/CAUTION when conditions "
                          "clear; the system will resume against the same plan."),
                source="operator",
                tags=["storm" if "storm" in reason else "fault",
                        "execution_block"],
            ))
        except Exception:
            self.log.exception("notification dispatch failed (non-fatal)")

    async def _update_verdict_from_weather(self, payload: dict) -> None:
        sev = payload.get("overall_severity", "ok")
        summary = payload.get("summary", "")
        if sev == "critical":
            verdict, reason = VERDICT_NOGO, summary or "Critical weather breach"
        elif sev == "warning":
            verdict, reason = VERDICT_CAUTION, summary or "Weather warning"
        elif sev == "ok":
            verdict, reason = VERDICT_GO, "Weather nominal."
        else:
            verdict, reason = VERDICT_UNKNOWN, "Weather assessment unavailable."

        new = OperatorVerdict(
            decided_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            verdict=verdict, reason=reason, sources=["critic.weather_assessment"],
        )
        prev = get_state().set_verdict(new)
        if prev is None or prev.verdict != verdict:
            self.log.info("Verdict changed: %s -> %s (%s)",
                            prev.verdict if prev else "—", verdict, reason)
            await self.bus.broadcast_event({
                "type": "verdict",
                "sender": "operator",
                "kind": "go_nogo",
                "verdict": verdict,
                "reason": reason,
                "previous": prev.verdict if prev else None,
                "sent_at": new.decided_at,
            })
            # Verdict change: dashboard sees this via the bus broadcast
            # above. NO sibling relay. The verdict gates EXECUTION only;
            # plan creation is independent. Sending a verdict_change
            # message to the Planner made it look like the Planner was
            # waiting on weather before creating a plan, which is the
            # opposite of how this is supposed to work. Bus-event only.
            await self._notify_agents_of_decision(
                kind="verdict_change",
                summary=(f"Verdict {prev.verdict if prev else '(none)'} -> "
                          f"{verdict}: {reason}"),
                to=(),
                verdict=verdict,
                previous=prev.verdict if prev else None,
                reason=reason,
                decided_at=new.decided_at,
            )

    async def _handle_alert(self, msg) -> None:
        severity = AlertSeverity(msg.payload.get("severity", "info"))
        code = msg.payload.get("code", "unknown")
        text = msg.payload.get("message", "")

        if self.safe_mode:
            # Conservative: log and surface; no autonomous corrective action
            self.log.warning("[safe-mode] alert pass-through: %s", code)
            return

        if get_state().is_manual():
            # Human is driving — surface the alert in the audit log but do
            # NOT auto-fix or trigger emergency shutdown sequences. Critical
            # safety alerts still escalate via broadcast so the dashboard
            # banner lights up; the human decides what to do about them.
            self.log.warning("[manual-control] alert pass-through: %s (%s)", code, severity)
            self.log_decision("alert_passthrough_manual",
                                inputs={"code": code, "severity": str(severity), "message": text},
                                rationale="Manual override engaged; not auto-fixing or shutting down",
                                session_id=self._current_session_id)
            try:
                await self.bus.broadcast_event({
                    "type": "alert_manual_passthrough",
                    "sender": "operator", "code": code,
                    "severity": severity.value if hasattr(severity, "value") else str(severity),
                    "message": text,
                    "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                })
            except Exception:
                pass
            return

        # Critical alerts → emergency-class decision
        if severity == AlertSeverity.CRITICAL:
            self.log.error("CRITICAL alert: %s — %s", code, text)
            await self._initiate_emergency_response(code, text)
            return

        # Auto-fixable alerts
        if code in ("focus_drift", "guiding_lost"):
            attempts = self._auto_fix_attempts.get(code, 0)
            if attempts < 2:
                self._auto_fix_attempts[code] = attempts + 1
                self.log.info("Auto-fix attempt %d/2 for %s", attempts + 1, code)
                # TODO Phase 2: invoke specific recovery routine
                return
            # Exceeded auto-fix budget — escalate
            self.log_decision("escalate_to_human", inputs={"code": code},
                              rationale=f"Auto-fix exceeded {attempts} attempts",
                              session_id=self._current_session_id)
            # TODO Phase 2: send ntfy.sh notification

        self.log.info("Acknowledging alert: %s", code)

    async def _initiate_emergency_response(self, code: str, message: str) -> None:
        self.log_decision(
            "emergency_response", inputs={"code": code, "message": message},
            rationale="Critical alert triggered emergency sequence",
            session_id=self._current_session_id,
        )
        if self._current_session_id is not None:
            SessionManager.set_state(self._current_session_id,
                                       SessionState.SHUTDOWN,
                                       reason=f"emergency: {code}")
        # TODO Phase 2: execute the shutdown sequence (atlas.safety.shutdown)
        await self.bus.broadcast_event({
            "type": "emergency", "code": code, "message": message,
        })

    async def _forward_to_planner(self, msg) -> None:
        await self.send(
            AgentName.PLANNER,
            kind=AgentMessageKind.REVISION_REQUEST,
            payload={"from": msg.sender.value if hasattr(msg.sender, "value") else msg.sender,
                      "details": msg.payload},
            session_id=self._current_session_id,
        )

    async def _notify_agents_of_decision(self, *, kind: str, summary: str,
                                                 to: tuple[AgentName, ...] = (),
                                                 **details) -> None:
        """Targeted STATUS relay to specific sibling agents.

        IMPORTANT: ``to`` defaults to () — no fan-out by default. Every
        call site must say exactly who needs to know. Earlier this
        defaulted to all four siblings, which made the dashboard look
        like every agent was repeating the same warning when really
        only the operator was making one decision.

        The dashboard's WebSocket bus already shows every decision
        as a broadcast_event with the source = "operator". Sending
        STATUS to a sibling on top of that should only happen when
        that sibling actually needs to react. The bus event is for
        the human; THIS path is for cross-agent reaction logic.

        Failures here never raise — siblings are best-effort
        informational."""
        if not to:
            return  # explicit empty list = bus-event only, no relay
        payload = {"kind": kind, "summary": summary, **details}
        for agent in to:
            try:
                await self.send(
                    agent, AgentMessageKind.STATUS,
                    payload=payload,
                    session_id=self._current_session_id,
                )
            except Exception as e:
                self.log.warning(
                    "Failed to relay '%s' status to %s: %s",
                    kind, agent.value if hasattr(agent, "value") else agent, e,
                )

    async def _handle_human_command(self, msg) -> None:
        """Dashboard-originated commands. Always execute. Dispatched by
        the command name. Each dispatch logs a decision row + broadcasts
        a flow event so the Mission Control feed shows the action."""
        cmd = (msg.payload.get("command") or "").lower().strip()
        params = msg.payload.get("params") or {}
        self.log.info("HUMAN COMMAND: %s %s", cmd, params)
        try:
            if cmd == "start_session":
                await self._cmd_start_session(params)
            elif cmd == "stop_session":
                await self._cmd_stop_session(params)
            elif cmd == "take_control":
                await self._cmd_take_control(params)
            elif cmd == "release_control":
                await self._cmd_release_control(params)
            elif cmd == "manual_action":
                await self._cmd_manual_action(params)
            elif cmd == "start_cooling":
                await self._cmd_start_cooling(params)
            elif cmd == "start_capture_sequence":
                await self._cmd_start_capture_sequence(params)
            elif cmd == "abort_capture_sequence":
                await self._cmd_abort_capture_sequence(params)
            else:
                self.log_decision("human_command_unrecognised",
                                    inputs={"command": cmd, "params": params},
                                    rationale="No dispatch for this command kind",
                                    session_id=self._current_session_id)
                self.log.warning("Operator command not recognised: %s", cmd)
        except Exception:
            self.log.exception("Operator command %s failed", cmd)

    async def _cmd_take_control(self, params: dict) -> None:
        """Engage human override. Operator stops dispatching autonomous
        session decisions, alert auto-fixes, and pipeline hand-offs until
        release_control. Pre-flight + Critic still publish status."""
        reason = (params.get("reason") or "operator requested control").strip()
        by = (params.get("by") or "operator").strip() or "operator"
        snap = get_state().set_manual_control(reason=reason, by=by)
        self.log_decision("take_control",
                            inputs={"reason": reason, "by": by},
                            outputs={"engaged_at": snap.engaged_at},
                            rationale=f"Human override engaged: {reason[:120]}",
                            session_id=self._current_session_id)
        self.set_task(f"manual control engaged by {by}: {reason[:60]}",
                      state="waiting")
        try:
            await self.bus.broadcast_event({
                "type": "manual_control",
                "sender": "operator",
                "engaged": True,
                "reason": reason,
                "by": by,
                "sent_at": snap.engaged_at,
            })
        except Exception:
            pass
        self.log.warning("Manual control ENGAGED by %s: %s", by, reason)

    async def _cmd_release_control(self, params: dict) -> None:
        """Release human override and let autonomy resume on the next
        pre-flight / message cycle."""
        reason = (params.get("reason") or "operator released control").strip()
        snap = get_state().clear_manual_control(reason=reason)
        self.log_decision("release_control",
                            inputs={"reason": reason},
                            outputs={"released_at": snap.released_at,
                                      "action_count": snap.action_count},
                            rationale=f"Human override released: {reason[:120]}",
                            session_id=self._current_session_id)
        self.set_task("autonomy resumed — standing by",
                      state="idle")
        try:
            await self.bus.broadcast_event({
                "type": "manual_control",
                "sender": "operator",
                "engaged": False,
                "reason": reason,
                "action_count": snap.action_count,
                "sent_at": snap.released_at,
            })
        except Exception:
            pass
        self.log.info("Manual control RELEASED: %s (%d manual actions recorded)",
                        reason, snap.action_count)

    async def _cmd_manual_action(self, params: dict) -> None:
        """Execute a single direct hardware command on behalf of the human.
        Only valid when manual control is engaged. Every action is logged +
        appended to the manual-actions ring buffer for the audit panel."""
        from atlas.config import is_simulation_mode
        from atlas.db.managers import ConfigManager
        kind = (params.get("kind") or "").lower().strip()
        args = params.get("args") or {}
        rationale = (params.get("rationale") or "").strip()
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

        if not get_state().is_manual():
            self.log.warning("manual_action %s rejected — not in manual mode", kind)
            self.log_decision("manual_action_rejected",
                                inputs={"kind": kind, "args": args},
                                rationale="Manual mode not engaged",
                                session_id=self._current_session_id)
            return

        # Pick the right client. Sim mode uses FakeNina (always succeeds);
        # real mode uses the NinaClient pointed at the configured host/port.
        equip = ConfigManager.get_equipment()
        if is_simulation_mode() or equip is None:
            from atlas.simulation.fake_hardware import FakeNina
            nina = FakeNina()
        else:
            from atlas.hardware.nina import NinaClient
            nina = NinaClient(host=equip.nina_host, port=equip.nina_port, timeout=10.0)

        ok = False
        result: dict = {}
        try:
            if kind == "slew":
                # Dashboard sends RA in hours (NINA's native unit) + DEC in degrees.
                ra_hours = float(args.get("ra_hours") if args.get("ra_hours") is not None
                                   else (float(args.get("ra_deg") or 0.0) / 15.0))
                dec_deg = float(args.get("dec_deg") or 0.0)
                result = await nina.slew(ra_hours=ra_hours, dec_deg=dec_deg)
                ok = bool(result.get("ok", True))
            elif kind == "park":
                result = await nina.park()
                ok = bool(result.get("ok", True))
            elif kind == "unpark":
                result = await nina.unpark()
                ok = bool(result.get("ok", True))
            elif kind == "capture":
                exposure_s = float(args.get("exposure_s") or 5.0)
                filter_name = args.get("filter") or None
                gain = args.get("gain")
                result = await nina.camera_capture(
                    exposure_s=exposure_s,
                    filter_name=filter_name,
                    gain=int(gain) if gain not in (None, "") else None,
                )
                ok = bool(result.get("ok", True))
            elif kind == "set_cooling":
                target_c = float(args.get("target_c") or -10.0)
                result = await nina.camera_set_cooling(target_c=target_c)
                ok = bool(result.get("ok", True))
            elif kind == "warmup":
                result = await nina.camera_warmup()
                ok = bool(result.get("ok", True))
            elif kind == "move_focuser":
                position = int(args.get("position") or 0)
                result = await nina.focuser_move(position=position)
                ok = bool(result.get("ok", True))
            elif kind == "change_filter":
                # NINA has no standalone "change filter" call — the filter
                # is selected as part of the next capture. We log the intent
                # so the audit trail still records it; a zero-exposure
                # snapshot would actually move the wheel but we don't want
                # to fire the shutter just to rotate.
                filt = args.get("filter") or "L"
                result = {"ok": True, "note": "filter latched for next capture",
                            "filter": filt}
                ok = True
            elif kind == "dome_open":
                result = await nina.dome_open()
                ok = bool(result.get("ok", True))
            elif kind == "dome_close":
                result = await nina.dome_close()
                ok = bool(result.get("ok", True))
            else:
                result = {"error": f"unknown manual_action kind: {kind}"}
                ok = False
        except Exception as e:
            self.log.exception("manual_action %s failed", kind)
            result = {"error": str(e)}
            ok = False
        finally:
            try:
                await nina.close()
            except Exception:
                pass

        action_record = {
            "at": now,
            "kind": kind,
            "args": args,
            "rationale": rationale or "(no rationale supplied)",
            "ok": ok,
            "result": result,
        }
        get_state().record_manual_action(action_record)
        self.log_decision("manual_action",
                            inputs={"kind": kind, "args": args,
                                      "rationale": rationale},
                            outputs={"ok": ok, "result": result},
                            rationale=f"manual {kind}: {rationale[:120]}",
                            session_id=self._current_session_id)
        self.set_task(f"manual {kind} {'ok' if ok else 'FAILED'} — {rationale[:60]}",
                      state="working")
        try:
            await self.bus.broadcast_event({
                "type": "manual_action",
                "sender": "operator",
                "kind": kind,
                "ok": ok,
                "rationale": rationale,
                "sent_at": now,
            })
        except Exception:
            pass

    async def _cmd_start_cooling(self, params: dict) -> None:
        """Cool the camera to the requested setpoint and wait for stable.
        Runs in a background task so the Operator's queue stays responsive
        (cooling can take 10-15 min on a warm night). Progress events are
        broadcast over the bus so the dashboard can show a live readout."""
        from atlas.config import is_simulation_mode
        from atlas.db.managers import ConfigManager
        from atlas.hardware.cooling import CoolingController
        target_c = float(params.get("target_c"))
        tolerance_c = float(params.get("tolerance_c") or 0.3)
        max_wait_s = float(params.get("max_wait_s") or 900.0)
        sim = is_simulation_mode()
        equip = ConfigManager.get_equipment()
        if sim or equip is None:
            from atlas.simulation.fake_hardware import FakeNina
            nina = FakeNina()
        else:
            from atlas.hardware.nina import NinaClient
            nina = NinaClient(host=equip.nina_host, port=equip.nina_port,
                                timeout=10.0)
        self.log_decision("start_cooling",
                            inputs={"target_c": target_c,
                                      "tolerance_c": tolerance_c},
                            rationale="Pre-imaging cooling sequence",
                            session_id=self._current_session_id)

        async def run() -> None:
            ctrl = CoolingController(
                nina, target_c=target_c, tolerance_c=tolerance_c,
                max_wait_s=max_wait_s, simulation=sim,
            )
            try:
                async for snap in ctrl.run():
                    get_state().push_agent_message("operator", {
                        "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        "kind": "cooling_progress",
                        **snap.to_jsonable(),
                    })
                    self.set_task(
                        f"cooling {snap.current_c:.2f}°C -> {snap.target_c:.1f}°C "
                        f"({snap.state})" if snap.current_c is not None
                        else f"cooling: {snap.state}",
                        state="working",
                    )
                    try:
                        await self.bus.broadcast_event({
                            "type": "cooling_progress",
                            "sender": "operator",
                            **snap.to_jsonable(),
                            "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        })
                    except Exception:
                        pass
                final = ctrl.final
                self.log_decision("cooling_complete",
                                    inputs={"target_c": target_c},
                                    outputs=final.to_jsonable() if final else {},
                                    rationale=final.note if final else "",
                                    session_id=self._current_session_id)
                self.set_task(
                    f"cooling {final.state}: {final.note}" if final
                    else "cooling finished",
                    state="idle",
                )
            finally:
                try:
                    await nina.close()
                except Exception:
                    pass

        asyncio.create_task(run(), name="operator-cooling")

    async def _cmd_start_capture_sequence(self, params: dict) -> None:
        """Walk a target's exposure plan, capturing frames via NINA. See
        atlas/capture/sequence.py. Runs in a background task; the
        Operator's queue keeps responding to commands during capture."""
        from atlas.capture.sequence import CaptureSequence
        from atlas.config import is_simulation_mode
        from atlas.db.managers import ConfigManager
        target = params.get("target") or {}
        exposure_plan = params.get("exposure_plan") or target.get("exposure_plan")
        dither_every = int(params.get("dither_every_n_frames") or 1)
        if not exposure_plan:
            self.log.warning("start_capture_sequence: no exposure_plan given")
            return
        sim = is_simulation_mode()
        equip = ConfigManager.get_equipment()
        if sim or equip is None:
            from atlas.simulation.fake_hardware import FakeNina, FakePhd2
            nina = FakeNina()
            phd2 = FakePhd2()
        else:
            from atlas.hardware.nina import NinaClient
            from atlas.hardware.phd2 import Phd2Client
            nina = NinaClient(host=equip.nina_host, port=equip.nina_port,
                                timeout=30.0)
            phd2 = Phd2Client(host=equip.phd2_host, port=equip.phd2_port,
                                timeout=10.0)
        # Resolve the workflow policy for this target. The workflow's
        # plan() returns a SequenceSpec carrying AutofocusPolicy +
        # PlateSolvePolicy + DitherPolicy + AcceptancePolicy — those
        # are the per-science-mode behavior differences (deepsky AFs
        # on filter change, exoplanet locks focus, astrometry solves
        # every frame, etc.).
        from atlas.workflows.registry import get_workflow
        wf_kind = (target.get("workflow") or "deepsky")
        try:
            wf = get_workflow(wf_kind)
            wf_spec = wf.plan(target=target, conditions={})
        except Exception as e:
            self.log.warning("workflow %s plan() failed; using deepsky "
                               "defaults: %s", wf_kind, e)
            wf = get_workflow("deepsky")
            wf_spec = wf.plan(target=target, conditions={})

        # Construct the autofocus engine straight from the workflow's
        # AutofocusPolicy. Every trigger comes from policy, no hardcoded
        # defaults at this layer.
        from atlas.capture.autofocus import AutofocusDecisionEngine
        af_pol = wf_spec.autofocus
        af_engine = AutofocusDecisionEngine(
            trigger_on_session_start=af_pol.before_sequence,
            trigger_on_filter_change=af_pol.on_filter_change,
            temp_delta_c=af_pol.temperature_delta_c,
            time_elapsed_min=(af_pol.time_interval_min
                                or 9999.0),  # None = effectively never
            hfr_factor=af_pol.hfr_drift_factor,
        )
        is_mono = bool(equip and getattr(equip, "camera_type", "OSC") == "MONO")

        # Plate-solve client gated by the workflow's PlateSolvePolicy.
        # Planetary turns this off entirely; everything else uses ASTAP.
        astap_client = None
        if wf_spec.platesolve.enabled:
            if not sim and equip is not None and getattr(equip, "astap_path", None):
                try:
                    from atlas.hardware.astap import AstapClient
                    astap_client = AstapClient(astap_path=equip.astap_path)
                except Exception as e:
                    self.log.warning("AstapClient construction failed; plate-solve "
                                       "disabled for this sequence: %s", e)
            elif sim:
                # Simulation mode: pass a sentinel so the sequence's
                # plate-solve gate fires and exercises the orchestrator
                # end-to-end (sim path returns synthetic success).
                from atlas.hardware.astap import AstapClient
                astap_client = AstapClient(astap_path=None)
        else:
            self.log.info("workflow %s disables plate-solve (e.g. planetary "
                            "ROI too small to solve)", wf_kind)

        # Dither cadence from workflow policy. enabled=False -> set
        # every_n_frames to a huge number so CaptureSequence skips
        # every check; enabled=True -> use the policy's cadence.
        dither_policy = wf_spec.dither
        dither_every_effective = (dither_policy.every_n_frames
                                     if dither_policy.enabled
                                     else 99999)

        seq = CaptureSequence(nina=nina, phd2=phd2, target=target,
                                exposure_plan=exposure_plan,
                                dither_every_n_frames=dither_every_effective,
                                simulation=sim,
                                session_id=self._current_session_id,
                                autofocus_engine=af_engine,
                                is_mono=is_mono,
                                astap_client=astap_client)
        # Stash the resolved spec on the sequence for the broadcast
        # event (dashboard shows which policies were applied).
        try:
            seq._workflow_spec = wf_spec  # type: ignore[attr-defined]
        except Exception:
            pass
        self._current_capture = seq
        self.log_decision("start_capture_sequence",
                            inputs={"target": target.get("target_name"),
                                      "plan_length": len(exposure_plan)},
                            rationale="Operator-initiated capture",
                            session_id=self._current_session_id)

        async def run() -> None:
            try:
                async for ev in seq.run():
                    get_state().push_agent_message("operator", {
                        "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        "kind": "capture_progress",
                        **ev,
                    })
                    self.set_task(
                        f"capture: {ev.get('summary', '')}",
                        state="working" if ev.get("state") != "complete" else "idle",
                    )
                    try:
                        await self.bus.broadcast_event({
                            "type": "capture_progress",
                            "sender": "operator",
                            "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                            **ev,
                        })
                    except Exception:
                        pass
                    # When the sequence flags needs_human=True (e.g.
                    # guiding or focus recovery exhausted all steps),
                    # page the operator through every configured
                    # notification channel.
                    if ev.get("needs_human"):
                        try:
                            from atlas.notifications.dispatcher import (
                                NotificationDispatcher,
                            )
                            from atlas.notifications.base import (
                                Notification,
                            )
                            dispatcher = NotificationDispatcher()
                            tgt = target.get("target_name") or "?"
                            steps = ev.get("steps_tried") or []
                            await dispatcher.dispatch(Notification(
                                severity="critical",
                                title=f"ATLAS needs you — "
                                        f"{ev.get('state','?')}",
                                message=ev.get("summary",
                                                 "auto-recovery exhausted"),
                                detail=(f"Target: {tgt}\n"
                                          f"State: {ev.get('state','?')}\n"
                                          f"Summary: {ev.get('summary','')}\n"
                                          f"Steps tried: {steps}"),
                                source="capture.recovery",
                                tags=["recovery", "escalated"],
                            ))
                        except Exception as e:
                            self.log.warning(
                                "notification dispatch failed: %s", e)
            finally:
                self._current_capture = None
                try:
                    await nina.close()
                except Exception:
                    pass
                try:
                    await phd2.close()
                except Exception:
                    pass

        asyncio.create_task(run(), name="operator-capture")

    async def _cmd_abort_capture_sequence(self, params: dict) -> None:
        seq = getattr(self, "_current_capture", None)
        if seq is None:
            self.log.info("abort_capture_sequence: nothing running")
            return
        seq.abort(reason=str(params.get("reason") or "operator-abort"))
        self.log_decision("abort_capture_sequence",
                            rationale=str(params.get("reason") or "operator-abort"),
                            session_id=self._current_session_id)

    async def _cmd_start_session(self, params: dict) -> None:
        """Create a new Session row in the DB, mark NOMINAL, broadcast.
        The new session_id is held in self._current_session_id so the
        Critic's fast loop + Archivist's POST_SESSION trigger have scope."""
        from atlas.config import is_simulation_mode
        simulation = bool(params.get("simulation", is_simulation_mode()))
        sid = SessionManager.start(simulation=simulation)
        SessionManager.set_state(sid, SessionState.NOMINAL,
                                   reason="Started by operator command")
        self._current_session_id = sid
        self.log_decision("session_started",
                            inputs={"params": params, "simulation": simulation},
                            outputs={"session_id": sid, "state": "nominal"},
                            rationale="Operator command start_session",
                            session_id=sid)
        self.set_task(f"session #{sid} started — imaging window open",
                      state="working")
        await self.bus.broadcast_event({
            "type": "session_started",
            "sender": "operator",
            "session_id": sid,
            "simulation": simulation,
            "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })
        # Archivist needs to know — prepares the watch-folder ingest
        # filter for this session_id. Other agents see the bus event.
        await self._notify_agents_of_decision(
            kind="session_started",
            summary=f"Session #{sid} started ({'sim' if simulation else 'live'})",
            to=(AgentName.ARCHIVIST,),
            session_id=sid, simulation=simulation, autonomous=False,
        )
        self.log.info("Session #%d started (simulation=%s)", sid, simulation)

    async def _cmd_stop_session(self, params: dict) -> None:
        """End the current session, mark COMPLETE, trigger Archivist."""
        sid = self._current_session_id or params.get("session_id")
        if sid is None:
            sess = SessionManager.latest()
            if sess is None or sess.state == SessionState.COMPLETE:
                self.log.warning("stop_session: no active session to stop")
                return
            sid = sess.id
        reason = params.get("reason") or "Stopped by operator command"
        SessionManager.set_state(sid, SessionState.COMPLETE, reason=reason)
        self.log_decision("session_stopped",
                            inputs={"session_id": sid, "reason": reason},
                            outputs={"state": "complete"},
                            rationale=reason,
                            session_id=sid)
        # Trigger the Archivist to process the session
        await self.send(
            AgentName.ARCHIVIST,
            AgentMessageKind.POST_SESSION,
            payload={"session_id": sid, "summary": f"Session #{sid} ended: {reason}"},
            session_id=sid,
        )
        self.set_task(f"session #{sid} ended — Archivist notified",
                      state="idle")
        self._current_session_id = None
        # Generate the morning report for this just-finished session.
        # Best-effort: never block the session-stop on the report.
        try:
            from atlas.reports.morning_report import write_morning_report
            path = write_morning_report(session_id=sid)
            if path is not None:
                self.log.info("morning report written: %s", path)
        except Exception as e:
            self.log.warning("morning report generation failed: %s", e)
        await self.bus.broadcast_event({
            "type": "session_stopped",
            "sender": "operator",
            "session_id": sid,
            "reason": reason,
            "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })
        # Archivist already got a POST_SESSION message above; bus
        # broadcast covers the dashboard. No additional sibling relay
        # needed — empty to=() means bus-event only.
        await self._notify_agents_of_decision(
            kind="session_stopped",
            summary=f"Session #{sid} stopped: {reason[:80]}",
            to=(),
            session_id=sid, reason=reason, autonomous=False,
        )
        self.log.info("Session #%d stopped: %s", sid, reason)

    async def _periodic_check(self) -> None:
        """Idle housekeeping. Runs roughly every 5 seconds when no messages."""
        # Reset auto-fix counters every hour
        # TODO Phase 2: implement once a real clock is plumbed
        return

    # --- safe-autonomous fallback ------------------------------------------

    async def safe_mode_step(self) -> None:
        """Deterministic rules when Claude API is unreachable:
        - Continue current target if one is active
        - Hold the schedule (no replans)
        - Reject non-trivial decisions
        - Surface API outage to the human
        """
        await asyncio.sleep(15)
