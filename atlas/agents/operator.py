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
            while not self.should_stop:
                msg = await self.recv_with_timeout(timeout_s=5.0)
                if msg is None:
                    await self._periodic_check()
                    continue
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
        """Run the comprehensive session-readiness pre-flight every 2
        minutes. Update shared state + broadcast the verdict on change."""
        from atlas.safety.preflight import run_session_preflight
        INTERVAL_S = 120
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
        """Status updates from other agents — primarily the Critic's
        weather assessment OR a SessionReview travelling through the
        chain-of-command pipeline."""
        payload = msg.payload or {}
        phase = payload.get("phase")
        if phase == "critic_review" and payload.get("review"):
            await self._route_to_oracle(payload["review"])
            return
        if phase == "oracle_review" and payload.get("review"):
            await self._decide_session(payload["review"])
            return
        kind = payload.get("kind")
        if kind == "weather_assessment":
            await self._update_verdict_from_weather(payload)
            return
        self.log.debug("Operator ignoring status kind=%s phase=%s", kind, phase)

    async def _route_to_oracle(self, review_dict: dict) -> None:
        """Phase 3 of pipeline: Operator hands the reviewed plan to Oracle
        for revisit / extended-integration analysis."""
        from atlas.agents.session_workflow import (
            SessionReview, PHASE_ORACLE_QUERY,
        )
        review = SessionReview.from_jsonable(review_dict)
        n_warn = sum(1 for w in review.critic_warnings if w.severity != "ok")
        self.set_task(
            f"routing plan {review.review_id} to Oracle ({n_warn} warning(s) from Critic)",
            state="working")
        review.advance(PHASE_ORACLE_QUERY, "operator",
                        note="forwarded critic review to Oracle for revisit check")
        get_state().set_session_review(review.to_jsonable())
        await self.send(
            AgentName.ORACLE, AgentMessageKind.STATUS,
            payload={
                "summary": (f"Plan {review.review_id} reviewed by Critic "
                              f"({n_warn} warning(s)). Please check for "
                              "revisits or targets needing extended integration."),
                "phase": PHASE_ORACLE_QUERY,
                "review": review.to_jsonable(),
                "from_chat": False,
            },
        )

    async def _decide_session(self, review_dict: dict) -> None:
        """Phase 5 of pipeline: Operator weighs the Critic warnings + Oracle
        suggestions and makes a final decision (proceed / re-plan / cancel),
        then hands back to the Planner to either finalise or rebuild."""
        from atlas.agents.session_workflow import (
            SessionReview, PHASE_OPERATOR_DECN,
        )
        review = SessionReview.from_jsonable(review_dict)

        critical = [w for w in review.critic_warnings if w.severity == "critical"]
        warnings = [w for w in review.critic_warnings if w.severity == "warning"]
        constraints: list[str] = []

        if any(w.kind == "hardware" for w in critical):
            decision = "cancel"
            reason = ("Hardware critical — "
                       + "; ".join(w.message for w in critical if w.kind == "hardware"))
        elif any(w.kind == "weather" and w.severity == "critical" for w in critical):
            decision = "cancel"
            reason = ("Weather critical — "
                       + "; ".join(w.message for w in critical if w.kind == "weather"))
        elif any(w.kind == "moon" and w.severity == "critical" for w in critical):
            decision = "replan"
            constraints.append("avoid_moon")
            reason = ("Moon critically impacts plan — re-plan avoiding "
                       "targets within 40° of the moon.")
        elif warnings:
            # warnings only → proceed but record constraints to inform next rebuild
            decision = "proceed"
            for w in warnings:
                if w.suggested_constraint and w.suggested_constraint not in constraints:
                    constraints.append(w.suggested_constraint)
            reason = (f"Proceeding with {len(warnings)} warning(s); "
                       f"constraints noted: {', '.join(constraints) or 'none'}")
        else:
            decision = "proceed"
            reason = "All gates clear; proceed with plan as-is."

        review.operator_decision = decision
        review.operator_constraints = constraints
        review.operator_reason = reason
        review.advance(PHASE_OPERATOR_DECN, "operator",
                        note=f"decision={decision}; {reason[:80]}")
        get_state().set_session_review(review.to_jsonable())
        self.set_task(f"decision: {decision.upper()} — {reason[:60]}",
                      state="waiting")
        self.log_decision("session_decision",
                            inputs={"review_id": review.review_id,
                                      "critical_count": len(critical),
                                      "warning_count": len(warnings),
                                      "oracle_suggestions_count": len(review.oracle_suggestions)},
                            outputs={"decision": decision,
                                      "constraints": constraints,
                                      "reason": reason},
                            rationale=reason)
        await self.send(
            AgentName.PLANNER, AgentMessageKind.STATUS,
            payload={
                "summary": (f"Decision on plan {review.review_id}: "
                              f"{decision.upper()} — {reason[:80]}"),
                "phase": PHASE_OPERATOR_DECN,
                "review": review.to_jsonable(),
                "from_chat": False,
            },
        )

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
        """Dashboard-originated commands. Always execute."""
        cmd = msg.payload.get("command")
        self.log.info("HUMAN COMMAND: %s", cmd)
        self.log_decision("human_command", inputs={"command": cmd, "params": msg.payload},
                           rationale="Operator command from dashboard",
                           session_id=self._current_session_id)
        # TODO Phase 2: dispatch to specific command handlers
        # commands: start_session, stop_session, take_control, release_control,
        # approve_target, run_simulation, emergency_stop, etc.

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
