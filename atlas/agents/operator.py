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


class Operator(BaseAgent):
    name = AgentName.OPERATOR

    def __init__(self) -> None:
        super().__init__()
        self._current_session_id: int | None = None
        self._auto_fix_attempts: dict[str, int] = {}  # code -> attempts
        # Register chat-time tools (weather, system status). Without these,
        # the dashboard's ATLAS-tab chat could only answer from training
        # knowledge — the Operator literally had no way to fetch live state.
        for spec in all_operator_tools():
            self.register_tool(spec)

    async def run(self) -> None:
        self.log.info("Operator agent online — final authority")
        self.set_task("standing by — final-authority watch on agent bus",
                      state="idle")
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
            try:
                await preflight_task
            except (asyncio.CancelledError, Exception):
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
          kind=critical_advisories  → evaluate hard-stop conditions
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
            await self._evaluate_hard_stop(payload)
            return
        self.log.debug("Operator ignoring status kind=%s", kind)

    async def _evaluate_hard_stop(self, payload: dict) -> None:
        """Decide whether incoming critical advisories warrant a
        hard-stop (autonomous session cancel).

        Hard-stops are reserved for things that could damage equipment
        or guarantee a wasted night:

          * precipitation > 0 in current weather  (storm)
          * wind > critical threshold             (mount/scope risk)
          * sustained 100% cloud across the whole dark window
            (no point opening the roof)
          * any hardware kind=critical advisory   (mount/camera fault)

        Operator-warning advisories (high humidity, moon proximity,
        dew margin tight) are NOT hard-stops — the operator decides
        whether to act on them via the dashboard banner.
        """
        if get_state().is_manual():
            self.log.info("Manual control engaged — skipping autonomous hard-stop evaluation.")
            self.log_decision("hard_stop_paused_manual",
                                rationale="Operator is driving; not auto-cancelling.")
            return

        advisories = payload.get("advisories") or []
        # Hard-stop triggers:
        hardware_fault = any(a.get("kind") == "hardware" for a in advisories)
        weather_storm = False
        # Look at current weather snapshot — precipitation > 0 is a storm.
        a = get_state().get_assessment()
        if a is not None:
            raw = a.raw_current or {}
            precip = raw.get("precip_in") or 0.0
            wind_mph = raw.get("wind_speed_mph") or 0.0
            # Wind hard-stop threshold (mph): a step above the user-set
            # "critical" wind. We're not second-guessing the threshold
            # the operator chose in Setup; we're saying "if wind is
            # already at critical, that IS a hard-stop."
            from atlas.safety.thresholds import SafetyThresholds
            t = SafetyThresholds.from_db()
            from atlas.units import ms_to_mph
            crit_mph = ms_to_mph(t.wind_speed_critical_ms)
            if precip > 0:
                weather_storm = True
            if wind_mph >= crit_mph:
                weather_storm = True   # treat as same class of risk

        if not (hardware_fault or weather_storm):
            self.log.info("Critical advisories filed but no hard-stop "
                            "conditions met — leaving plan READY.")
            self.log_decision("hard_stop_evaluated",
                                inputs={"advisories": advisories},
                                outputs={"hard_stop": False},
                                rationale="No storm / damage-risk indicators")
            return

        # Hard-stop fires. Flip the live plan into HARD_STOP, broadcast,
        # and log.
        from atlas.agents.session_workflow import SessionPlanState
        live = get_state().get_session_review()
        reasons = []
        if hardware_fault:
            reasons.append("hardware critical")
        if weather_storm:
            reasons.append("storm / extreme wind")
        reason = " + ".join(reasons)
        if live is not None:
            try:
                plan = SessionPlanState.from_jsonable(live)
                plan.hard_stop(reason, source="operator")
                get_state().set_session_review(plan.to_jsonable())
            except Exception:
                self.log.exception("Failed to mark plan hard-stopped")
        self.log.warning("HARD STOP: %s", reason)
        self.set_task(f"HARD STOP: {reason}", state="safe-mode")
        self.log_decision("session_hard_stop",
                            inputs={"advisories": advisories,
                                      "reason": reason},
                            rationale=reason)
        await self.bus.broadcast_event({
            "type": "hard_stop",
            "sender": "operator",
            "reason": reason,
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
