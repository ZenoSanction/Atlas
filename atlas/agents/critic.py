"""Critic agent — continuous watchdog. Never decides; only reports.

Two loops:
  - Fast loop (90s): guiding RMS, focus HFR, frame quality — imaging only.
                     Currently a heartbeat pending Phase 2 NINA/PHD2 polls.
  - Standard loop (300s): weather, dew margin, wind, cloud cover, humidity,
                          precipitation. Pulled live from Open-Meteo. The
                          per-metric assessment is written to shared state
                          and sent to the Operator as a STATUS message so
                          the chain-of-command ("Critic reports, Operator
                          decides") stays intact.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from atlas.agents.base import BaseAgent
from atlas.agents.state import MetricCheck, WeatherAssessment, get_state
from atlas.db.managers import AlertManager, ConfigManager, SessionManager
from atlas.db.models import AgentMessageKind, AgentName, AlertSeverity
from atlas.safety.thresholds import SafetyThresholds
from atlas.units import (
    c_to_f, c_delta_to_f, fmt_f, fmt_f_delta, fmt_in, fmt_mph,
    ms_to_mph, mm_to_in,
)
from atlas.weather.openmeteo import OpenMeteoClient, WeatherSnapshot


FAST_LOOP_S = 90
STANDARD_LOOP_S = 300
FORECAST_HOURS = 12


# Severity-rank helper -------------------------------------------------------
_SEV_RANK = {"ok": 0, "warning": 1, "critical": 2}


def _max_sev(*severities: str) -> str:
    return max(severities, key=lambda s: _SEV_RANK.get(s, 0))


def _check_wind(snap: WeatherSnapshot, t: SafetyThresholds) -> MetricCheck:
    v = snap.wind_speed_ms
    v_mph = ms_to_mph(v)
    if v >= t.wind_speed_critical_ms:
        return MetricCheck("wind", "critical", v, t.wind_speed_critical_ms,
                            f"{v_mph:.1f} mph ≥ critical {ms_to_mph(t.wind_speed_critical_ms):.1f} mph")
    if v >= t.wind_speed_warn_ms:
        return MetricCheck("wind", "warning", v, t.wind_speed_warn_ms,
                            f"{v_mph:.1f} mph ≥ warn {ms_to_mph(t.wind_speed_warn_ms):.1f} mph")
    return MetricCheck("wind", "ok", v, t.wind_speed_warn_ms,
                        f"calm ({v_mph:.1f} mph)")


def _check_dew_margin(snap: WeatherSnapshot, t: SafetyThresholds) -> MetricCheck:
    dm_c = snap.temperature_c - snap.dew_point_c
    dm_f = c_delta_to_f(dm_c)
    if dm_c <= t.dew_margin_critical_c:
        return MetricCheck("dew_margin", "critical", dm_c, t.dew_margin_critical_c,
                            f"{dm_f:.1f}°F ≤ critical {c_delta_to_f(t.dew_margin_critical_c):.1f}°F")
    if dm_c <= t.dew_margin_warn_c:
        return MetricCheck("dew_margin", "warning", dm_c, t.dew_margin_warn_c,
                            f"{dm_f:.1f}°F ≤ warn {c_delta_to_f(t.dew_margin_warn_c):.1f}°F")
    return MetricCheck("dew_margin", "ok", dm_c, t.dew_margin_warn_c,
                        f"{dm_f:.1f}°F — comfortable")


def _check_humidity(snap: WeatherSnapshot, t: SafetyThresholds) -> MetricCheck:
    # Humidity already unit-agnostic (%)
    v = snap.humidity_pct
    if v >= t.humidity_critical_pct:
        return MetricCheck("humidity", "critical", v, t.humidity_critical_pct,
                            f"{v:.0f}% ≥ critical {t.humidity_critical_pct:.0f}%")
    if v >= t.humidity_warn_pct:
        return MetricCheck("humidity", "warning", v, t.humidity_warn_pct,
                            f"{v:.0f}% ≥ warn {t.humidity_warn_pct:.0f}%")
    return MetricCheck("humidity", "ok", v, t.humidity_warn_pct, f"{v:.0f}%")


def _check_cloud(snap: WeatherSnapshot, t: SafetyThresholds) -> MetricCheck:
    v = snap.cloud_cover_pct
    if v >= t.cloud_cover_critical_pct:
        return MetricCheck("cloud_cover", "critical", v, t.cloud_cover_critical_pct,
                            f"{v:.0f}% ≥ critical {t.cloud_cover_critical_pct:.0f}%")
    if v >= t.cloud_cover_warn_pct:
        return MetricCheck("cloud_cover", "warning", v, t.cloud_cover_warn_pct,
                            f"{v:.0f}% ≥ warn {t.cloud_cover_warn_pct:.0f}%")
    return MetricCheck("cloud_cover", "ok", v, t.cloud_cover_warn_pct, f"{v:.0f}%")


def _check_precip(snap: WeatherSnapshot) -> MetricCheck:
    v_mm = snap.precip_mm
    v_in = mm_to_in(v_mm)
    if v_mm >= 0.1:
        return MetricCheck("precip", "critical", v_mm, 0.1,
                            f"{v_in:.3f} in in the last hour — close the roof")
    return MetricCheck("precip", "ok", v_mm, 0.0, "dry")


def _hourly_severity(rows: list[dict], t: SafetyThresholds) -> list[dict]:
    """Light per-hour shading for the dashboard. Imperial display fields."""
    out = []
    for r in rows:
        dm_c = r["temperature_c"] - r["dew_point_c"]
        sev = "ok"
        if dm_c <= t.dew_margin_critical_c or r["cloud_cover_pct"] >= t.cloud_cover_critical_pct \
                or r["wind_speed_ms"] >= t.wind_speed_critical_ms \
                or r["precip_mm"] >= 0.1:
            sev = "critical"
        elif dm_c <= t.dew_margin_warn_c or r["cloud_cover_pct"] >= t.cloud_cover_warn_pct \
                or r["wind_speed_ms"] >= t.wind_speed_warn_ms:
            sev = "warning"
        out.append({"time_utc": r["time"], "severity": sev,
                     "dew_margin_f": round(c_delta_to_f(dm_c), 1),
                     "cloud_cover_pct": round(r["cloud_cover_pct"], 0),
                     "wind_speed_mph": round(ms_to_mph(r["wind_speed_ms"]), 1),
                     "precip_in": round(mm_to_in(r["precip_mm"]), 3)})
    return out


def _summary_from_checks(checks: list[MetricCheck], overall: str) -> str:
    breaches = [c for c in checks if c.severity != "ok"]
    if not breaches:
        return "All weather metrics nominal."
    parts = []
    for c in breaches:
        parts.append(f"{c.metric.replace('_', ' ')} {c.severity} ({c.note})")
    return "; ".join(parts)


class Critic(BaseAgent):
    name = AgentName.CRITIC

    def __init__(self) -> None:
        super().__init__()
        self._last_fast = 0.0
        self._last_standard = 0.0
        self._alert_state: dict[str, int] = {}  # code -> consecutive_count
        self._initial_done = False
        # _thresholds is reloaded from DB on each tick so Setup edits take
        # effect at the next standard loop without restart.
        from atlas.agents.critic_tools import CRITIC_TOOLS
        for spec in CRITIC_TOOLS:
            self.register_tool(spec)

    async def run(self) -> None:
        self.log.info("Critic agent online (fast %ds, standard %ds)",
                       FAST_LOOP_S, STANDARD_LOOP_S)
        self.set_task("watchdog online — first weather pull next",
                      state="working")
        # Background task: drain the bus queue so relays to the Critic
        # (e.g., Planner asking for a fresh weather review) actually get
        # picked up. Until now Critic never read from its queue.
        drain_task = asyncio.create_task(self._drain_bus(), name="critic-bus-drain")
        try:
            while not self.should_stop:
                now = asyncio.get_event_loop().time()
                # Force an initial standard tick on startup so the dashboard
                # has data without waiting 5 minutes.
                if not self._initial_done:
                    self._initial_done = True
                    try:
                        await self._standard_loop()
                    except Exception:
                        self.log.exception("Initial standard loop failed")
                    self._last_standard = asyncio.get_event_loop().time()
                if now - self._last_fast >= FAST_LOOP_S:
                    await self._fast_loop()
                    self._last_fast = now
                if now - self._last_standard >= STANDARD_LOOP_S:
                    try:
                        await self._standard_loop()
                    except Exception:
                        self.log.exception("Standard loop failed")
                    self._last_standard = asyncio.get_event_loop().time()
                # Publish next-tick estimates so Mission Control can show countdowns
                self._publish_next_ticks(now)
                await asyncio.sleep(5)
        finally:
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _drain_bus(self) -> None:
        """Drain the Critic's bus queue. On inbound relays, react to the
        ones we recognise; otherwise fall back to the BaseAgent default
        (log + broadcast a 'received' event)."""
        while not self.should_stop:
            msg = await self.recv_with_timeout(timeout_s=5.0)
            if msg is None:
                continue
            try:
                await self._handle_relay(msg)
            except Exception:
                self.log.exception("Critic relay handler failed")

    async def _handle_relay(self, msg) -> None:
        """Inbound relay handler. Always surfaces the message to the
        dashboard first, then dispatches by phase / kind:

          phase=plan_built     → full session review (weather + moon +
                                  hardware) → forward to Operator
          kind=revision_request → on-demand standard loop
          kind=status (no phase) → on-demand standard loop
        """
        await self.handle_relayed_message(msg)
        payload = msg.payload or {}
        phase = payload.get("phase")

        if phase == "plan_built" and payload.get("review"):
            await self._review_session_plan(payload["review"])
            return

        kind = msg.kind.value if hasattr(msg.kind, "value") else str(msg.kind)
        if kind in ("revision_request", "status"):
            try:
                await self._standard_loop()
            except Exception:
                self.log.exception("On-demand standard loop failed")
            self._last_standard = asyncio.get_event_loop().time()

    async def _review_session_plan(self, review_dict: dict) -> None:
        """Step 2 of the session pipeline: review the plan for weather,
        moon position vs. each visible target, and hardware readiness.
        Append warnings to the SessionReview, advance to phase=critic_review,
        and forward to the Operator."""
        from atlas.agents.session_workflow import (
            SessionReview, SessionWarning, PHASE_CRITIC_REVIEW,
        )
        from atlas.astronomy import (
            angular_separation, compute_alt_az, moon_position,
        )
        from datetime import datetime as _dt

        review = SessionReview.from_jsonable(review_dict)
        self.set_task(f"reviewing plan {review.review_id}: weather + moon + hardware",
                      state="working")

        # Make sure our weather assessment is fresh
        try:
            await self._standard_loop()
        except Exception:
            self.log.exception("Standard loop on review failed")
        self._last_standard = asyncio.get_event_loop().time()

        # 1. Weather → pull from shared state (just-refreshed)
        a = get_state().get_assessment()
        if a is not None:
            for c in a.checks:
                if c.severity in ("warning", "critical"):
                    review.critic_warnings.append(SessionWarning(
                        kind="weather",
                        severity=c.severity,
                        message=f"{c.metric.replace('_',' ')}: {c.note}",
                        suggested_constraint=("avoid_low_alt"
                                                if c.metric == "dew_margin"
                                                else None),
                    ))

        # 2. Moon — illumination + per-target separation
        site = ConfigManager.get_site()
        if site is not None:
            now = _dt.utcnow()
            try:
                moon_ra, moon_dec, illum = moon_position(now)
                moon_alt, _ = compute_alt_az(moon_ra, moon_dec,
                                               float(site.latitude),
                                               float(site.longitude), now)
            except Exception as e:
                self.log.warning("Moon position failed: %s", e)
                moon_ra = moon_dec = illum = moon_alt = None

            if illum is not None and moon_alt is not None:
                # Only flag moon impact when moon is up AND bright (>30% illum)
                if moon_alt > 0 and illum > 0.30:
                    targets = review.plan.get("visible_targets") or []
                    close_targets = []
                    for t in targets:
                        if t.get("ra_deg") is None or t.get("dec_deg") is None:
                            continue
                        sep = angular_separation(
                            float(t["ra_deg"]), float(t["dec_deg"]),
                            moon_ra, moon_dec,
                        )
                        if sep < 40.0:   # within 40° of bright moon
                            close_targets.append((t["target_name"], sep))
                    if close_targets:
                        names = ", ".join(f"{n} ({s:.0f}°)" for n, s in close_targets[:5])
                        sev = "warning" if illum < 0.7 else "critical"
                        review.critic_warnings.append(SessionWarning(
                            kind="moon",
                            severity=sev,
                            message=(f"Moon {illum*100:.0f}% illuminated, alt {moon_alt:.0f}°. "
                                       f"{len(close_targets)} target(s) within 40°: {names}"),
                            suggested_constraint="avoid_moon",
                        ))
                    else:
                        review.critic_warnings.append(SessionWarning(
                            kind="moon",
                            severity="ok",
                            message=(f"Moon {illum*100:.0f}% illum, alt {moon_alt:.0f}° — "
                                       "no plan targets within 40°."),
                        ))
                else:
                    if moon_alt <= 0:
                        note = "below horizon"
                    elif illum <= 0.30:
                        note = "too faint to interfere"
                    else:
                        note = "no impact"
                    review.critic_warnings.append(SessionWarning(
                        kind="moon",
                        severity="ok",
                        message=(f"Moon {illum*100:.0f}% illum, alt {moon_alt:.0f}° — "
                                  f"{note}."),
                    ))

        # 3. Hardware — reuse the cached snapshot from routes
        try:
            from atlas.api.routes import _HARDWARE_SNAPSHOT_CACHE
            snap = _HARDWARE_SNAPSHOT_CACHE.get("data") or {}
            offline = [k for k, v in snap.items()
                        if not v.get("connected") and v.get("status") != "n/a"
                        and k != "guiding"]
            if offline:
                review.critic_warnings.append(SessionWarning(
                    kind="hardware",
                    severity="critical",
                    message=f"Disconnected: {', '.join(offline)}",
                ))
        except Exception:
            pass

        # Advance phase and forward to Operator
        sev_counts = {"ok": 0, "warning": 0, "critical": 0}
        for w in review.critic_warnings:
            sev_counts[w.severity] = sev_counts.get(w.severity, 0) + 1
        review.advance(PHASE_CRITIC_REVIEW, "critic",
                        note=(f"{sev_counts['critical']} critical, "
                                f"{sev_counts['warning']} warning, "
                                f"{sev_counts['ok']} ok"))
        get_state().set_session_review(review.to_jsonable())

        await self.send(
            AgentName.OPERATOR, AgentMessageKind.STATUS,
            payload={
                "summary": (f"Reviewed plan {review.review_id}: "
                              f"{sev_counts['critical']} critical, "
                              f"{sev_counts['warning']} warning, "
                              f"{sev_counts['ok']} ok"),
                "phase": PHASE_CRITIC_REVIEW,
                "review": review.to_jsonable(),
                "from_chat": False,
            },
        )
        self.set_task(f"plan {review.review_id} forwarded to Operator",
                      state="idle")

    def _publish_next_ticks(self, now_monotonic: float) -> None:
        """Compute when the next fast + standard loops will fire (in wall
        UTC) and publish to shared state so the dashboard can render a
        live countdown."""
        from datetime import datetime, timedelta
        next_fast_s = max(0.0, FAST_LOOP_S - (now_monotonic - self._last_fast))
        next_std_s = max(0.0, STANDARD_LOOP_S - (now_monotonic - self._last_standard))
        # Whichever fires sooner is what we surface as the "next tick"
        if next_fast_s < next_std_s:
            next_at = datetime.utcnow() + timedelta(seconds=next_fast_s)
            kind = "fast_loop"
        else:
            next_at = datetime.utcnow() + timedelta(seconds=next_std_s)
            kind = "standard_loop"
        from atlas.agents.state import get_state
        get_state().update_agent_status(
            "critic",
            next_tick_at=next_at.isoformat(timespec="seconds") + "Z",
            next_tick_kind=kind,
        )

    async def _fast_loop(self) -> None:
        """Fast loop: guiding RMS, focus HFR, camera temperature.
        Only runs when a session is actively imaging."""
        sess = SessionManager.latest()
        if sess is None:
            self.set_task("fast loop: no active session — skipping",
                          state="idle")
            return
        state = sess.state.value if hasattr(sess.state, "value") else sess.state
        if state not in ("nominal", "warning"):
            self.set_task(
                f"fast loop: session state '{state}' — skipping checks",
                state="idle")
            return
        self.set_task("fast loop: PHD2 guiding + NINA cooling check",
                      state="working")

        from atlas.config import is_simulation_mode
        if is_simulation_mode():
            # In sim mode the fast loop ticks but doesn't try to pull from
            # the fake hardware (the fakes don't expose guiding stats).
            self.log.debug("fast loop tick (sim)")
            return

        equip = ConfigManager.get_equipment()
        if equip is None:
            return
        session_id = sess.id

        # ---- Guiding RMS (PHD2) ------------------------------------------
        try:
            from atlas.hardware.phd2 import Phd2Client
            async with Phd2Client(host=equip.phd2_host, port=equip.phd2_port,
                                    timeout=3.0) as phd2:
                stats = await phd2.call("get_star_image")  # cheap reachability probe
                # Try to pull guiding stats
                try:
                    gstats = await phd2.call("get_guide_stats")
                    rms = float(gstats.get("rms_total", 0.0))
                    if rms > 4.0:
                        await self._raise(AlertSeverity.CRITICAL, "guiding_lost",
                                            f"Guiding RMS {rms:.2f}\" > 4.0\"",
                                            session_id=session_id,
                                            data={"rms_total": rms})
                    elif rms > 2.0:
                        await self._raise(AlertSeverity.WARNING, "guiding_drift",
                                            f"Guiding RMS {rms:.2f}\" > 2.0\"",
                                            session_id=session_id,
                                            data={"rms_total": rms})
                    else:
                        self._clear("guiding_lost")
                        self._clear("guiding_drift")
                except Exception:
                    pass
        except Exception as e:
            self.log.debug("PHD2 fast-loop poll failed: %s", e)

        # ---- Camera temperature + focuser HFR (NINA) ---------------------
        try:
            from atlas.hardware.nina import NinaClient
            async with NinaClient(host=equip.nina_host, port=equip.nina_port,
                                    timeout=3.0) as nina:
                cam = await nina.camera_info()
                temp = cam.get("temperature") if isinstance(cam, dict) else None
                setpoint = float(equip.cooling_setpoint_c)
                if temp is not None and abs(float(temp) - setpoint) > 3.0:
                    await self._raise(AlertSeverity.WARNING, "cooling_drift",
                                        f"CCD temp {temp:.1f}°C drifted >3°C from setpoint {setpoint:.1f}°C",
                                        session_id=session_id,
                                        data={"temperature_c": temp,
                                                "setpoint_c": setpoint})
                else:
                    self._clear("cooling_drift")
                # Focuser HFR — NINA exposes this if focusing has run
                # TODO Phase 2: pull last-known HFR from NINA history once
                # the Advanced API endpoint is wired through nina.py
        except Exception as e:
            self.log.debug("NINA fast-loop poll failed: %s", e)

        # Broadcast a lightweight tick so the dashboard sees the fast loop
        # actually running.
        await self.bus.broadcast_event({
            "type": "assessment",
            "sender": "critic",
            "kind": "fast_loop_tick",
            "severity": "ok",
            "summary": "Guiding + cooling checked.",
            "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })

    async def _standard_loop(self) -> None:
        """Standard loop: weather pull + per-metric assessment + push to Operator."""
        self.set_task("standard loop: pulling Open-Meteo current + forecast",
                      state="working")
        site = ConfigManager.get_site()
        if site is None:
            self.set_task("standard loop: no site config yet — skipping",
                          state="idle")
            self.log.debug("standard loop: no site config yet, skipping")
            return

        client = OpenMeteoClient(latitude=float(site.latitude),
                                  longitude=float(site.longitude))
        try:
            snap = await client.current()
            forecast_rows = await client.forecast_hours(hours=FORECAST_HOURS)
        except Exception as e:
            self.set_task(f"standard loop: Open-Meteo failed ({e})",
                          state="idle")
            self.log.warning("Open-Meteo fetch failed: %s", e)
            return

        # Pull live thresholds from DB so Setup-tab edits apply immediately
        t = SafetyThresholds.from_db()
        checks = [
            _check_wind(snap, t),
            _check_dew_margin(snap, t),
            _check_humidity(snap, t),
            _check_cloud(snap, t),
            _check_precip(snap),
        ]
        overall = "ok"
        for c in checks:
            overall = _max_sev(overall, c.severity)

        dm_c = snap.temperature_c - snap.dew_point_c
        assessment = WeatherAssessment(
            observed_at=snap.observed_at,
            assessed_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            overall_severity=overall,
            summary=_summary_from_checks(checks, overall),
            checks=checks,
            raw_current={
                # Imperial (display) — what the dashboard + chat tools use
                "temperature_f": round(c_to_f(snap.temperature_c), 1),
                "dew_point_f": round(c_to_f(snap.dew_point_c), 1),
                "dew_margin_f": round(c_delta_to_f(dm_c), 1),
                "wind_speed_mph": round(ms_to_mph(snap.wind_speed_ms), 1),
                "wind_gust_mph": (round(ms_to_mph(snap.wind_gust_ms), 1)
                                    if snap.wind_gust_ms is not None else None),
                "pressure_inhg": round(snap.pressure_hpa * 0.02953, 2),
                "precip_in": round(mm_to_in(snap.precip_mm), 3),
                # Unit-agnostic
                "humidity_pct": snap.humidity_pct,
                "cloud_cover_pct": snap.cloud_cover_pct,
                # SI originals retained for any internal calculation
                "_si": {
                    "temperature_c": snap.temperature_c,
                    "dew_point_c": snap.dew_point_c,
                    "dew_margin_c": round(dm_c, 1),
                    "wind_speed_ms": snap.wind_speed_ms,
                    "wind_gust_ms": snap.wind_gust_ms,
                    "pressure_hpa": snap.pressure_hpa,
                    "precip_mm": snap.precip_mm,
                },
            },
            hourly_severity=_hourly_severity(forecast_rows, t),
        )

        # 1) Park in shared state for the HTTP layer
        get_state().set_assessment(assessment)

        # 2) Tell the Operator (chain of command: Critic reports, Operator decides)
        await self.send(
            AgentName.OPERATOR, AgentMessageKind.STATUS,
            payload={"kind": "weather_assessment",
                      "overall_severity": overall,
                      "summary": assessment.summary,
                      "checks": [{"metric": c.metric, "severity": c.severity,
                                    "value": c.value, "threshold": c.threshold,
                                    "note": c.note} for c in checks]},
        )

        # 3) Broadcast to dashboard so the Agent Activity feed shows the Critic
        #    actually working (instead of silent heartbeats).
        await self.bus.broadcast_event({
            "type": "assessment",
            "sender": "critic",
            "kind": "weather_assessment",
            "severity": overall,
            "summary": assessment.summary,
            "sent_at": assessment.assessed_at,
        })

        # 4) Persist alerts for breaches so the Tonight tab Alerts card lights up
        sess = SessionManager.latest()
        session_id = sess.id if sess else None
        for c in checks:
            if c.severity == "critical":
                await self._raise(AlertSeverity.CRITICAL, f"weather_{c.metric}",
                                    c.note, session_id=session_id,
                                    data={"value": c.value,
                                            "threshold": c.threshold})
            elif c.severity == "warning":
                await self._raise(AlertSeverity.WARNING, f"weather_{c.metric}",
                                    c.note, session_id=session_id,
                                    data={"value": c.value,
                                            "threshold": c.threshold})
            else:
                self._clear(f"weather_{c.metric}")

        self.log.info("standard loop: overall=%s (%s)", overall, assessment.summary)
        self.set_task(
            f"standard loop done — overall {overall}; next sweep in ~5 min",
            state="waiting")

    async def _raise(self, severity: AlertSeverity, code: str, message: str,
                     session_id: int | None = None, data: dict | None = None,
                     escalate_on_repeats: int = 3) -> None:
        """Deduplicate-aware alert raise."""
        prev = self._alert_state.get(code, 0)
        self._alert_state[code] = prev + 1
        # First-time, or escalation, or every N-th repeat
        if prev == 0 or prev == escalate_on_repeats:
            AlertManager.raise_alert(severity, code, message, AgentName.CRITIC,
                                      session_id=session_id, data=data)
            await self.send(
                AgentName.OPERATOR, AgentMessageKind.ALERT,
                payload={"severity": severity.value, "code": code,
                          "message": message, "data": data or {}},
                session_id=session_id,
            )

    def _clear(self, code: str) -> None:
        if code in self._alert_state:
            del self._alert_state[code]

    async def safe_mode_step(self) -> None:
        # Critic continues monitoring even when Claude is unreachable —
        # its core function is sensor reading, not language reasoning.
        await asyncio.sleep(30)
