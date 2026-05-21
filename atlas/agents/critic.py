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
# Bumped from 5 min to 15 min. The Critic no longer drives weather
# pulls — it reads from WeatherCache, which refreshes on a 15 min TTL.
# The standard loop's job is now just "re-assess against the cached
# snapshot if the cache rotated, otherwise idle". Network round-trips
# drop from ~12/hr to ~4/hr per agent.
STANDARD_LOOP_S = 900
FORECAST_HOURS = 12
# Sun-altitude cutoff for "astronomical night". Reports + per-hour
# severity entries only cover hours where the sun is below this. -18°
# matches the IAU definition of astronomical twilight (full darkness).
ASTRO_DARK_ALT_DEG = -18.0


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


def _hourly_severity(rows: list[dict], t: SafetyThresholds,
                       lat: float | None = None,
                       lon: float | None = None) -> list[dict]:
    """Light per-hour shading for the dashboard. Imperial display fields.

    If ``lat`` and ``lon`` are supplied, hours during the day (sun above
    ASTRO_DARK_ALT_DEG = -18°) are dropped entirely — the dashboard's
    forecast table only shows the imaging-usable window."""
    from atlas.astronomy import sun_altitude
    out = []
    for r in rows:
        if lat is not None and lon is not None:
            try:
                t_iso = datetime.fromisoformat(r["time"])
                if sun_altitude(lat, lon, t_iso) >= ASTRO_DARK_ALT_DEG:
                    continue  # daytime hour — not relevant for imaging
            except Exception:
                pass
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
        # PHD2 event-stream subscriber. Owned by the Critic for its
        # lifetime; the fast loop reads rolling RMS off the in-memory
        # buffer instead of RPC'ing PHD2 every 90 s. Sim mode synthesises
        # events; real mode opens a TCP connection on equip.phd2_host.
        try:
            from atlas.config import is_simulation_mode
            from atlas.hardware.phd2_events import get_event_stream
            sim = is_simulation_mode()
            equip = ConfigManager.get_equipment()
            if equip is not None:
                self._phd2_stream = get_event_stream(
                    host=equip.phd2_host, port=equip.phd2_port,
                    simulation=sim,
                )
            else:
                self._phd2_stream = get_event_stream(
                    host="localhost", port=4400, simulation=sim,
                )
        except Exception as e:
            self.log.warning("PHD2 event stream not started: %s", e)
            self._phd2_stream = None
        # Initial standard tick so the dashboard has data immediately.
        try:
            await self._standard_loop()
        except Exception:
            self.log.exception("Initial standard loop failed")
        self._initial_done = True
        self._last_standard = asyncio.get_event_loop().time()

        # Sibling tasks: periodic timer (fast + standard ticks) and bus
        # drain (events). Both block on real conditions — no polling.
        periodic_task = asyncio.create_task(self._periodic_loop(),
                                               name="critic-periodic")
        try:
            # Event loop = bus drain. Block until a message arrives.
            await self._drain_bus()
        finally:
            periodic_task.cancel()
            try:
                from atlas.hardware.phd2_events import stop_event_stream
                await stop_event_stream()
            except Exception:
                pass
            try:
                await periodic_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _periodic_loop(self) -> None:
        """Fast (PHD2 buffer scan) + standard (cache freshness +
        re-assessment) ticks. Sleeps the *minimum* of the two cadences
        between iterations and checks elapsed time — no busy-loop, no
        5-second polling, no work done unless something is actually
        due. Standard loop is now keyed to the WeatherCache TTL, so on
        a steady night it fires roughly every 15 min."""
        # Initial brief offset so the startup standard_loop has time
        # to settle into shared state before fast_loop reads from it.
        await asyncio.sleep(15)
        while not self.should_stop:
            now = asyncio.get_event_loop().time()
            try:
                if now - self._last_fast >= FAST_LOOP_S:
                    await self._fast_loop()
                    self._last_fast = asyncio.get_event_loop().time()
            except Exception:
                self.log.exception("fast loop failed")
            try:
                if now - self._last_standard >= STANDARD_LOOP_S:
                    await self._standard_loop()
                    self._last_standard = asyncio.get_event_loop().time()
            except Exception:
                self.log.exception("standard loop failed")
            self._publish_next_ticks(asyncio.get_event_loop().time())
            # Sleep until the next tick is due. Pick the smaller of
            # the two upcoming deadlines, with a 5s floor so we don't
            # tight-loop on a clock-jitter edge case.
            now = asyncio.get_event_loop().time()
            next_fast = self._last_fast + FAST_LOOP_S - now
            next_std = self._last_standard + STANDARD_LOOP_S - now
            await asyncio.sleep(max(5.0, min(next_fast, next_std)))

    async def _drain_bus(self) -> None:
        """Event-driven bus drain. Blocks on recv() until a message
        arrives — no 5-second polling tick — so an inbound relay
        (e.g. operator forcing a weather refresh) wakes the Critic
        instantly."""
        while not self.should_stop:
            try:
                msg = await self.recv()
            except (asyncio.CancelledError, RuntimeError):
                break
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
        """Fast loop: guiding RMS (from PHD2 event stream), camera
        temperature, focus HFR. Only runs when a session is actively
        imaging."""
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
        sim = is_simulation_mode()
        equip = ConfigManager.get_equipment()
        session_id = sess.id

        # ---- Guiding RMS (PHD2 event stream, in both real + sim) --------
        # The Phd2EventStream singleton has been folding GuideStep events
        # into a rolling buffer since boot. We just compute current RMS
        # in arcseconds (using the equipment's pixel scale if available)
        # and compare against thresholds.
        if self._phd2_stream is not None:
            pixel_scale = None
            if equip and getattr(equip, "pixel_scale_arcsec", None):
                try:
                    pixel_scale = float(equip.pixel_scale_arcsec)
                except (TypeError, ValueError):
                    pixel_scale = None
            stats = self._phd2_stream.current(pixel_scale_arcsec=pixel_scale)
            # Prefer arcseconds when we have pixel scale, fall back to
            # pixels otherwise. Threshold: 2"/4" RMS-total in arcsec
            # (standard amateur thresholds at long focal length); in
            # pixel-only fallback we use 1.5px / 3px.
            metric = stats.rms_total_arcsec
            unit = '"'
            warn_threshold, crit_threshold = 2.0, 4.0
            if metric is None:
                metric = stats.rms_total_px
                unit = "px"
                warn_threshold, crit_threshold = 1.5, 3.0

            # Stale buffer = nothing arriving from PHD2 — guiding probably
            # stopped or PHD2 lost the socket.
            buffer_stale = (stats.last_step_age_s > 30 and stats.sample_count > 0)
            if buffer_stale:
                await self._raise(
                    AlertSeverity.CRITICAL, "guiding_silent",
                    f"PHD2 buffer stale ({stats.last_step_age_s:.0f}s "
                    f"since last GuideStep). Guiding may have stopped.",
                    session_id=session_id, data=stats.to_jsonable(),
                )
            elif stats.sample_count == 0:
                # Never received a sample — cold boot or PHD2 offline.
                # Don't alert (the GO/NO-GO hardware gate handles that).
                self._clear("guiding_silent")
                self._clear("guiding_drift")
                self._clear("guiding_lost")
            elif stats.star_lost_recent:
                await self._raise(
                    AlertSeverity.CRITICAL, "guiding_lost",
                    "PHD2 emitted StarLost in the last 30 s",
                    session_id=session_id, data=stats.to_jsonable(),
                )
            elif metric > crit_threshold:
                await self._raise(
                    AlertSeverity.CRITICAL, "guiding_lost",
                    f"Guiding RMS {metric:.2f}{unit} > {crit_threshold:.1f}{unit}",
                    session_id=session_id, data=stats.to_jsonable(),
                )
            elif metric > warn_threshold:
                await self._raise(
                    AlertSeverity.WARNING, "guiding_drift",
                    f"Guiding RMS {metric:.2f}{unit} > {warn_threshold:.1f}{unit}",
                    session_id=session_id, data=stats.to_jsonable(),
                )
            else:
                self._clear("guiding_silent")
                self._clear("guiding_lost")
                self._clear("guiding_drift")
            # Park the stats in shared state so the dashboard's Critic
            # lane can display the live RMS reading.
            try:
                get_state().push_agent_message("critic", {
                    "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "kind": "guiding_rms",
                    "rms_total_px": stats.rms_total_px,
                    "rms_total_arcsec": stats.rms_total_arcsec,
                    "sample_count": stats.sample_count,
                    "app_state": stats.app_state,
                })
            except Exception:
                pass

        if sim:
            self.log.debug("fast loop tick (sim) — guiding from event stream")
            return

        if equip is None:
            return

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
        """Standard loop: weather pull + per-metric assessment + push to Operator.

        Behaviour is *darkness-aware*. Outside the astronomical dark window
        (sun above -18°), we still pull weather so the dashboard's "now"
        snapshot is current, but we DON'T raise warning-level alerts on
        wind / cloud / dew margin / humidity — none of that matters when
        the telescope is parked for the day. Critical-class breaches
        (precip, extreme wind) still escalate because they're safety
        issues regardless of session state.
        """
        self.set_task("standard loop: pulling Open-Meteo current + forecast",
                      state="working")
        site = ConfigManager.get_site()
        if site is None:
            self.set_task("standard loop: no site config yet — skipping",
                          state="idle")
            self.log.debug("standard loop: no site config yet, skipping")
            return

        # Are we in the astronomical dark window right now?
        from atlas.astronomy import sun_altitude, night_window
        lat = float(site.latitude); lon = float(site.longitude)
        now = datetime.utcnow()
        sun_alt = sun_altitude(lat, lon, now)
        is_dark = sun_alt < ASTRO_DARK_ALT_DEG
        next_dark_iso: str | None = None
        if not is_dark:
            nw = night_window(lat, lon, now, altitude_deg=ASTRO_DARK_ALT_DEG)
            if nw is not None:
                next_dark_iso = nw[0].isoformat(timespec="seconds") + "Z"

        # Read through WeatherCache rather than hitting Open-Meteo
        # directly. The cache refreshes on its own 15-min TTL; the
        # Critic just consumes whatever's there. Force-refresh is only
        # used by the Planner before building a new session plan — for
        # the Critic, "what does the cache currently say" is the right
        # question at every tick.
        from atlas.weather.cache import get_weather_cache
        cache = get_weather_cache()
        try:
            state = await cache.get(lat=lat, lon=lon,
                                       forecast_hours=FORECAST_HOURS)
        except Exception as e:
            self.set_task(f"standard loop: weather cache failed ({e})",
                          state="idle")
            self.log.warning("Weather cache get failed: %s", e)
            return
        if state.snapshot is None:
            self.set_task("standard loop: no weather data available yet",
                          state="idle")
            return
        snap = state.snapshot
        forecast_rows = state.forecast_hours or []
        if state.refreshed_this_call:
            self.log.debug("weather cache miss -> refreshed inside Critic")
        else:
            self.log.debug("weather cache hit (age=%ss)", state.age_seconds)

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
        # During daytime, drop the severity to "ok" for the Operator's
        # verdict (no NO-GO at 2 pm), but keep the per-metric checks so
        # the dashboard's Critic Assessment card still shows what's
        # currently breached for context.
        effective_overall = overall
        summary_text = _summary_from_checks(checks, overall)
        if not is_dark and overall != "critical":
            effective_overall = "ok"
            summary_text = (f"daytime — sun {sun_alt:.1f}° above "
                              f"astronomical dark; weather warnings suppressed "
                              f"until {next_dark_iso or 'dusk'}")
        assessment = WeatherAssessment(
            observed_at=snap.observed_at,
            assessed_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            overall_severity=effective_overall,
            summary=summary_text,
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
            hourly_severity=_hourly_severity(
                forecast_rows, t,
                lat=float(site.latitude), lon=float(site.longitude),
            ),
        )

        # 1) Park in shared state for the HTTP layer
        get_state().set_assessment(assessment)

        # 2) Tell the Operator (chain of command: Critic reports, Operator decides)
        #    Send the *effective* severity so the Operator's verdict path
        #    doesn't flip to CAUTION/NO-GO during daytime warnings.
        await self.send(
            AgentName.OPERATOR, AgentMessageKind.STATUS,
            payload={"kind": "weather_assessment",
                      "overall_severity": effective_overall,
                      "summary": assessment.summary,
                      "is_dark": is_dark,
                      "next_dark_utc": next_dark_iso,
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
            "severity": effective_overall,
            "summary": assessment.summary,
            "is_dark": is_dark,
            "sent_at": assessment.assessed_at,
        })

        # 4) Persist alerts for breaches. During daytime (sun above -18°)
        #    we suppress warning-level alerts — wind/cloud/dew don't
        #    matter when the telescope is parked. Critical breaches
        #    (precip / extreme wind) still escalate because those are
        #    safety issues regardless of imaging state.
        sess = SessionManager.latest()
        session_id = sess.id if sess else None
        for c in checks:
            if c.severity == "critical":
                await self._raise(AlertSeverity.CRITICAL, f"weather_{c.metric}",
                                    c.note, session_id=session_id,
                                    data={"value": c.value,
                                            "threshold": c.threshold})
            elif c.severity == "warning" and is_dark:
                await self._raise(AlertSeverity.WARNING, f"weather_{c.metric}",
                                    c.note, session_id=session_id,
                                    data={"value": c.value,
                                            "threshold": c.threshold})
            else:
                # ok severity, OR daytime warning — clear any prior raise.
                self._clear(f"weather_{c.metric}")

        if is_dark:
            self.log.info("standard loop: overall=%s (%s)",
                            overall, assessment.summary)
            self.set_task(
                f"standard loop done — overall {overall}; next sweep in ~5 min",
                state="waiting")
        else:
            dark_at = next_dark_iso or "?"
            self.log.info("standard loop: daytime (sun alt %.1f°) — "
                            "warnings suppressed, next dark at %s",
                            sun_alt, dark_at)
            self.set_task(
                f"daytime: warnings suppressed; next dark window at {dark_at}",
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
