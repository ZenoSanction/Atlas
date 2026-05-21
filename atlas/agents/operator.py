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
        # Register chat-time tools (weather, system status). Without these,
        # the dashboard's ATLAS-tab chat could only answer from training
        # knowledge — the Operator literally had no way to fetch live state.
        for spec in all_operator_tools():
            self.register_tool(spec)

    async def run(self) -> None:
        self.log.info("Operator agent online — final authority")
        self.set_task("standing by — final-authority watch on agent bus",
                      state="idle")
        # Verdict-transition watcher. Runs every 30 seconds; reads the
        # current OperatorVerdict from shared state and decides whether
        # to fire safe-shutdown (NO-GO) or safe-startup + replan (GO/
        # CAUTION after the hysteresis). This is the loop that turns
        # the verdict into real hardware action.
        self._verdict_watcher_task = asyncio.create_task(
            self._verdict_watcher_loop(),
            name="operator-verdict-watcher",
        )
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
                    self.set_task("standing by — last action handled", state="idle")
                except Exception:
                    self.log.exception("Operator failed handling message: %s", msg.kind)
                    self.set_task("error handling last message — see log",
                                  state="idle")
        finally:
            preflight_task.cancel()
            if self._verdict_watcher_task is not None:
                self._verdict_watcher_task.cancel()
            try:
                await preflight_task
            except (asyncio.CancelledError, Exception):
                pass
            if self._verdict_watcher_task is not None:
                try:
                    await self._verdict_watcher_task
                except (asyncio.CancelledError, Exception):
                    pass

    # ---- Verdict watcher: convert verdict transitions into actions -----

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
            # 15s cadence (was 30s). Transition from CAUTION → NO-GO
            # now drives a shutdown within ~15s of the Critic noticing,
            # not 30s+. Cheap loop: reads shared-state only, no IO.
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
        self.log.debug("Operator ignoring status kind=%s", kind)

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

        if not (hardware_fault or weather_storm):
            self.log.info("Critical advisories filed but no execution-"
                            "block conditions met — verdict unchanged.")
            self.log_decision("execution_block_evaluated",
                                inputs={"advisories": advisories},
                                outputs={"blocked": False},
                                rationale="No storm / damage-risk indicators")
            return

        # Execution block fires. Flip the verdict to NO-GO. The
        # _verdict_watcher_loop will detect this transition and
        # autonomously fire SafeShutdownSequence + park the session.
        # The plan itself stays READY — operator can still review
        # what was planned, and when weather clears the watcher
        # handles startup + replan against the same plan.
        reasons = []
        if hardware_fault:
            reasons.append("hardware critical")
        if weather_storm:
            reasons.append("storm / extreme wind")
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
        seq = CaptureSequence(nina=nina, phd2=phd2, target=target,
                                exposure_plan=exposure_plan,
                                dither_every_n_frames=dither_every,
                                simulation=sim,
                                session_id=self._current_session_id)
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
        await self.bus.broadcast_event({
            "type": "session_stopped",
            "sender": "operator",
            "session_id": sid,
            "reason": reason,
            "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })
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
