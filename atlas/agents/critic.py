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
# Standard loop is the "expensive" pass: full forecast pull + per-hour
# severity. It runs no more often than the cache TTL allows. Adaptive
# mode (set on the cache from this loop) tightens the cache TTL when
# imaging is active or borderline.
STANDARD_LOOP_S = 90    # poll frequency; cache TTL gates the actual refresh
FORECAST_HOURS = 12
# Sun-altitude cutoff for "astronomical night". Reports + per-hour
# severity entries only cover hours where the sun is below this. -18°
# matches the IAU definition of astronomical twilight (full darkness).
ASTRO_DARK_ALT_DEG = -18.0
# A metric is "borderline" when it has reached this fraction of its
# critical threshold — within 20% means we tighten the polling mode.
BORDERLINE_FRACTION = 0.80


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
            # Fast-loop adaptive cadence: when there's no active
            # session, PHD2 RMS / NINA cooling checks are pure waste
            # (nothing to monitor). Stretch the effective interval to
            # 5 min in that case. Saves ~700 fast-loop wake-ups/day
            # when the observatory is idle (most daytime hours).
            active_fast_interval = FAST_LOOP_S
            try:
                from atlas.db.managers import SessionManager
                sess = SessionManager.latest()
                if sess is None:
                    active_fast_interval = 300.0
                else:
                    state = sess.state.value if hasattr(sess.state, "value") else sess.state
                    if state not in ("nominal", "warning"):
                        active_fast_interval = 300.0
            except Exception:
                pass
            try:
                if now - self._last_fast >= active_fast_interval:
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
            next_fast = self._last_fast + active_fast_interval - now
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
                self._mark_msg_handled(msg, ok=True)
            except Exception as e:
                self.log.exception("Critic relay handler failed")
                self._mark_msg_handled(msg, ok=False,
                                          error=f"{type(e).__name__}: {e}")

    async def _handle_relay(self, msg) -> None:
        """Inbound relay handler. Surfaces the message + dispatches:

          kind=plan_review (phase=critic) → file advisories, FORWARD
                                              to Operator (sequential
                                              review chain stage 2)
          kind=plan_advisory_request      → legacy parallel-fanout path
                                              (still supported)
          kind=revision_request           → on-demand weather refresh
          kind=status (no kind tag)       → on-demand weather refresh
        """
        await self.handle_relayed_message(msg)
        payload = msg.payload or {}

        # Sequential review chain (operator-specified): file advisories
        # then forward to Operator with phase="operator". The Critic
        # does NOT need to be asked to do this — it reads the plan out
        # of the payload, runs its weather/moon/hardware/cloud checks,
        # and hands off automatically.
        if (payload.get("kind") == "plan_review"
              and payload.get("phase") == "critic"
              and payload.get("review")):
            review = payload["review"]
            plan = (review or {}).get("plan") or {}
            n_targets = len(plan.get("visible_targets") or [])
            self.set_task(
                f"Stage 2/5: Critic auto-reviewing plan "
                f"({n_targets} target(s)) — weather + moon + hardware",
                state="working",
            )
            try:
                await self.bus.broadcast_event({
                    "type": "review_chain_stage",
                    "sender": "critic",
                    "stage": "2/5",
                    "agent": "critic",
                    "phase": "critic",
                    "review_id": payload.get("review_id"),
                    "n_targets": n_targets,
                    "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                })
            except Exception:
                pass
            await self._file_plan_advisories(review)
            try:
                from atlas.agents.state import get_state
                get_state().set_review_phase(
                    "operator", review_id=payload.get("review_id"))
                from atlas.db.models import AgentMessageKind, AgentName
                await self.send(
                    AgentName.OPERATOR, AgentMessageKind.STATUS,
                    payload={
                        "summary": (f"Stage 2 complete — Critic auto-"
                                      f"reviewed {n_targets} target(s); "
                                      "handing off to Operator."),
                        "kind": "plan_review",
                        "phase": "operator",
                        "review_id": payload.get("review_id"),
                        "review": payload.get("review"),
                    },
                )
            except Exception:
                self.log.exception("Failed to forward review chain to "
                                      "Operator stage")
            return

        # Legacy parallel-fanout path (still functional for any
        # non-chain advisory request that comes in)
        if (payload.get("kind") == "plan_advisory_request"
              and payload.get("review")):
            await self._file_plan_advisories(payload["review"])
            return

        kind = msg.kind.value if hasattr(msg.kind, "value") else str(msg.kind)
        if kind in ("revision_request", "status"):
            try:
                await self._standard_loop()
            except Exception:
                self.log.exception("On-demand standard loop failed")
            self._last_standard = asyncio.get_event_loop().time()

    async def _file_plan_advisories(self, review_dict: dict) -> None:
        """File weather + moon + hardware advisories against a plan.

        ZERO BLOCKING. The Critic acts on whatever data it already
        has in shared state at the moment the Planner hands it the
        plan — it does not wait for a fresh weather pull, a fresh
        hardware snapshot, or anything else. If the data is stale or
        missing, it says so via an info-level advisory so the
        operator can judge whether to act on it. Speed > freshness
        precision: when something is about to break, we don't make
        it worse by sitting on a check until the next poll cycle.

        Adds advisories asynchronously; never gates the plan. The
        plan is already READY — this is purely additive context.
        """
        from atlas.agents.session_workflow import (
            SessionPlanState, Advisory,
        )
        from atlas.astronomy import (
            angular_separation, compute_alt_az, moon_position,
        )
        from datetime import datetime as _dt

        # Build a LIST of advisories (don't touch the SessionPlanState
        # directly). At the end we hand the list to
        # get_state().append_advisories(review_id, list) which is
        # atomic — Critic and Oracle can file concurrently against
        # the same plan without overwriting each other.
        review_id = review_dict.get("review_id")
        plan = review_dict.get("plan") or {}
        self.log.info("filing advisories for plan %s", review_id)
        self.set_task(
            f"filing advisories for plan {review_id}",
            state="working",
        )

        now_iso = _dt.utcnow().isoformat(timespec="seconds") + "Z"
        now_dt = _dt.utcnow()
        criticals: list[Advisory] = []
        my_advisories: list[Advisory] = []
        def _add(adv: Advisory) -> None:
            my_advisories.append(adv)

        # 1. Weather — read from shared state at this instant. No
        #    refresh, no wait. If nothing is there yet (cold boot,
        #    first plan), surface that as an info advisory so the
        #    operator knows the absence is real, not silent.
        a = get_state().get_assessment()
        if a is not None:
            self.log.info("advisory pass: assessment has %d checks, "
                          "severities=%s, overall=%s",
                          len(a.checks),
                          [c.severity for c in a.checks],
                          a.overall_severity)
        if a is None:
            _add(Advisory(
                kind="weather", severity="info",
                message=("no weather assessment yet — Critic standard "
                          "loop hasn't completed first pass. Advisory "
                          "will fill in within ~90 s."),
                source="critic", at=now_iso,
            ))
        else:
            assessed_age_s: float | None = None
            try:
                assessed_dt = _dt.fromisoformat(a.assessed_at.rstrip("Z"))
                assessed_age_s = (now_dt - assessed_dt).total_seconds()
            except Exception:
                pass
            if assessed_age_s is not None and assessed_age_s > 300:
                _add(Advisory(
                    kind="weather", severity="info",
                    message=(f"acting on weather assessment "
                              f"{assessed_age_s/60:.1f} min old; refresh "
                              "scheduled on next cache TTL boundary."),
                    source="critic", at=now_iso,
                ))
            for c in a.checks:
                if c.severity in ("warning", "critical"):
                    adv = Advisory(
                        kind="weather", severity=c.severity,
                        message=f"{c.metric.replace('_',' ')}: {c.note}",
                        source="critic", at=now_iso,
                        suggested_constraint=("avoid_low_alt"
                                                if c.metric == "dew_margin"
                                                else None),
                    )
                    _add(adv)
                    if c.severity == "critical":
                        criticals.append(adv)

        # 2. Moon — illumination + per-target separation
        site = ConfigManager.get_site()
        if site is not None:
            try:
                moon_ra, moon_dec, illum = moon_position(_dt.utcnow())
                moon_alt, _ = compute_alt_az(moon_ra, moon_dec,
                                               float(site.latitude),
                                               float(site.longitude),
                                               _dt.utcnow())
            except Exception as e:
                self.log.warning("Moon position failed: %s", e)
                moon_ra = moon_dec = illum = moon_alt = None

            if illum is not None and moon_alt is not None:
                if moon_alt > 0 and illum > 0.30:
                    targets = plan.get("visible_targets") or []
                    close = []
                    for t in targets:
                        if t.get("ra_deg") is None or t.get("dec_deg") is None:
                            continue
                        sep = angular_separation(
                            float(t["ra_deg"]), float(t["dec_deg"]),
                            moon_ra, moon_dec,
                        )
                        if sep < 40.0:
                            close.append((t["target_name"], sep))
                    if close:
                        names = ", ".join(f"{n} ({s:.0f}°)" for n, s in close[:5])
                        sev = "warning"
                        _add(Advisory(
                            kind="moon", severity=sev,
                            message=(f"Moon {illum*100:.0f}% illum, "
                                       f"alt {moon_alt:.0f}°. "
                                       f"{len(close)} target(s) within 40°: {names}"),
                            source="critic", at=now_iso,
                            suggested_constraint="avoid_moon",
                        ))

        # 3. Hardware — reuse the cached snapshot from routes
        try:
            from atlas.api.routes import _HARDWARE_SNAPSHOT_CACHE
            snap = _HARDWARE_SNAPSHOT_CACHE.get("data") or {}
            offline = [k for k, v in snap.items()
                        if not v.get("connected") and v.get("status") != "n/a"
                        and k != "guiding"]
            if offline:
                adv = Advisory(
                    kind="hardware", severity="critical",
                    message=f"Disconnected: {', '.join(offline)}",
                    source="critic", at=now_iso,
                )
                _add(adv)
                criticals.append(adv)
        except Exception:
            pass

        # 4. Cloud-cover forecast over the coming dark window. If the
        #    weather forecast says the whole night is going to be
        #    socked in, file a critical advisory tagged
        #    "session_wasted_forecast". The Operator's autonomous-
        #    start logic checks for this and skips the night rather
        #    than pointlessly cooling the camera + opening the roof
        #    just to capture clouds. Manual start still works (the
        #    operator can override if they want to try).
        try:
            await self._check_cloudover_forecast(
                plan, _add, criticals, now_iso,
            )
        except Exception:
            self.log.exception("cloudover-forecast check failed")

        # Atomic append — survives concurrent writes from Oracle's
        # parallel advisory pass against the same plan.
        from dataclasses import asdict
        accepted = get_state().append_advisories(
            review_id,
            [asdict(a) for a in my_advisories],
        )
        if not accepted:
            self.log.info("plan %s already rotated; advisories discarded",
                          review_id)

        # Notify the Operator ONLY for critical advisories — those are
        # the ones that might warrant a hard-stop.
        if criticals:
            await self.send(
                AgentName.OPERATOR, AgentMessageKind.STATUS,
                payload={
                    "kind": "critical_advisories",
                    "review_id": review_id,
                    "advisory_count": len(criticals),
                    "advisories": [{
                        "kind": a.kind, "severity": a.severity,
                        "message": a.message, "source": a.source,
                    } for a in criticals],
                    "summary": (f"{len(criticals)} critical "
                                  f"advisor{'ies' if len(criticals) > 1 else 'y'} "
                                  f"on plan {review_id}"),
                },
            )
        sev_counts = {"info": 0, "warning": 0, "critical": 0}
        for adv in my_advisories:
            if adv.severity in sev_counts:
                sev_counts[adv.severity] += 1
        self.set_task(
            f"plan {review_id} reviewed: "
            f"{sev_counts['critical']} crit / {sev_counts['warning']} warn / "
            f"{sev_counts['info']} info",
            state="idle",
        )

    async def _check_cloudover_forecast(self, plan: dict, add_fn,
                                            criticals: list,
                                            now_iso: str) -> None:
        """Look at the forecast over the plan's effective dark window.
        If average cloud cover ≥ 85% AND every hour ≥ 75%, file a
        critical 'session_wasted_forecast' advisory.

        The two thresholds together filter out:
          * Single bad hours surrounded by clear ones (avg trips but
            min doesn't — we'd lose some hours but still get useful
            data)
          * Broken cloud nights that the average doesn't capture
            (avg looks fine but one bad hour makes a single target
            unusable — but other targets still imageable)

        Both conditions must trip to pull the trigger. Conservative
        on purpose — calling a night dead and shutting down is a
        heavy decision; we want to be confident the forecast is
        actually that bad.
        """
        window = plan.get("window") or {}
        if not window.get("dusk_utc") or not window.get("dawn_utc"):
            return
        from atlas.weather.cache import get_weather_cache
        from datetime import datetime as _dt
        cache_state = get_weather_cache().peek()
        rows = cache_state.forecast_hours or []
        if not rows:
            return
        try:
            dusk_dt = _dt.fromisoformat(
                window["dusk_utc"].rstrip("Z"))
            dawn_dt = _dt.fromisoformat(
                window["dawn_utc"].rstrip("Z"))
            now = _dt.utcnow()
            effective_start = max(dusk_dt, now)
        except Exception:
            return
        in_window = []
        for r in rows:
            try:
                t = _dt.fromisoformat(r["time"].rstrip("Z"))
            except Exception:
                continue
            if effective_start <= t < dawn_dt:
                cloud = r.get("cloud_cover_pct")
                if cloud is not None:
                    in_window.append(float(cloud))
        if len(in_window) < 3:
            # Not enough forecast coverage in the window — can't
            # confidently call it. Silent skip.
            return
        avg = sum(in_window) / len(in_window)
        min_cov = min(in_window)
        if avg >= 85.0 and min_cov >= 75.0:
            from atlas.agents.session_workflow import Advisory
            adv = Advisory(
                kind="session_wasted_forecast", severity="critical",
                message=(
                    f"Forecast shows cloudover throughout tonight's "
                    f"dark window: avg {avg:.0f}% across {len(in_window)}h, "
                    f"minimum {min_cov:.0f}%. Autonomous-start "
                    f"will skip; manual start still permitted."
                ),
                source="critic", at=now_iso,
            )
            add_fn(adv)
            criticals.append(adv)

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

        # ---- Disk free space (cheap; runs every fast tick) --------------
        # If frames_dir's volume drops critically low mid-session, the
        # capture sequence will fail on the next FITS write. We catch
        # the trend early and either warn (≤5 GB) or escalate to
        # critical (≤1 GB). Critical fires through the alert -> verdict
        # path that the watcher already handles — Operator's
        # _evaluate_execution_block sees hardware-class criticals and
        # stops the session before the disk actually fills.
        try:
            import shutil
            from atlas.config import get_settings
            frames_dir = get_settings().frames_dir
            if frames_dir is not None:
                _total, _used, free = shutil.disk_usage(str(frames_dir))
                free_gb = free / 1_000_000_000.0
                if free_gb <= 1.0:
                    await self._raise(
                        AlertSeverity.CRITICAL, "disk_critical_low",
                        f"Frames volume has only {free_gb:.2f} GB free "
                        f"(< 1 GB). Capture will fail soon.",
                        session_id=session_id,
                        data={"free_gb": round(free_gb, 2),
                                "path": str(frames_dir)},
                    )
                    # Also route through the execution-block path so
                    # the Operator's verdict watcher fires the
                    # SafeShutdownSequence (mount park, camera warm).
                    # Direct alert alone would only set SHUTDOWN state
                    # without actually parking the hardware.
                    await self.send(
                        AgentName.OPERATOR, AgentMessageKind.STATUS,
                        payload={
                            "kind": "critical_advisories",
                            "review_id": "runtime_disk",
                            "advisory_count": 1,
                            "advisories": [{
                                "kind": "disk", "severity": "critical",
                                "source": "critic",
                                "message": (
                                    f"Frames volume {free_gb:.2f} GB "
                                    f"free; capture will fail soon."
                                ),
                            }],
                            "summary": "disk runtime critical",
                        },
                    )
                elif free_gb <= 5.0:
                    await self._raise(
                        AlertSeverity.WARNING, "disk_low",
                        f"Frames volume has {free_gb:.1f} GB free "
                        f"(< 5 GB).",
                        session_id=session_id,
                        data={"free_gb": round(free_gb, 1),
                                "path": str(frames_dir)},
                    )
                else:
                    self._clear("disk_low")
                    self._clear("disk_critical_low")
        except Exception:
            self.log.exception("disk-space check failed")

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

        # ---- Moon proximity to the currently-imaged target -----------
        # Run-time equivalent of the plan-time moon check. If a target
        # is being imaged AND the moon has risen since the plan was
        # built (or the target has slewed close to it), surface an
        # advisory. Quality concern, not safety — never blocks
        # execution, just informs the operator.
        try:
            await self._check_moon_proximity_runtime(session_id)
        except Exception:
            self.log.exception("moon proximity runtime check failed")

        if sim:
            self.log.debug("fast loop tick (sim) — guiding from event stream")
            return

        if equip is None:
            return

        # ---- Camera temperature + focuser HFR (from hardware cache) -----
        # Read from the dashboard's hardware-snapshot cache (10s TTL,
        # 4s hard-timeout when refreshing) rather than blocking the
        # fast loop on a fresh NINA call. The cache is already kept
        # current by API route reads + the Critic's own fast loop in
        # earlier iterations. If something has gone really wrong with
        # NINA, the hardware GO/NO-GO gate handles it from a different
        # code path.
        try:
            from atlas.api.routes import _HARDWARE_SNAPSHOT_CACHE
            hw_snap = _HARDWARE_SNAPSHOT_CACHE.get("data") or {}
            cam = hw_snap.get("camera") or {}
            temp = cam.get("temperature")
            setpoint = float(equip.cooling_setpoint_c)
            if temp is not None and abs(float(temp) - setpoint) > 3.0:
                await self._raise(
                    AlertSeverity.WARNING, "cooling_drift",
                    f"CCD temp {temp:.1f}°C drifted >3°C from "
                    f"setpoint {setpoint:.1f}°C",
                    session_id=session_id,
                    data={"temperature_c": temp,
                            "setpoint_c": setpoint},
                )
            else:
                self._clear("cooling_drift")
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

    async def _check_moon_proximity_runtime(self, session_id: int) -> None:
        """If a target is being imaged, compute moon position + the
        target's current separation. Surface an advisory when the
        moon is up, bright (>30% illum), and within 30° — quality
        concern, never an execution block.

        Severity bumps to warning when the operator has no narrowband
        filter installed (Halpha/OIII/SII) to fight back against
        moonlight. With NB filters even a full moon is workable for
        the targets that emit in those lines."""
        from atlas.astronomy import (
            angular_separation, compute_alt_az, moon_position,
        )
        from atlas.db.managers import ConfigManager
        from atlas.db.models import Frame
        from atlas.db.session import get_session
        from datetime import datetime as _dt

        site = ConfigManager.get_site()
        if site is None:
            return
        # Find the active target — most recent frame on this session
        # is the strongest signal of "what are we currently on?"
        with get_session() as s:
            row = (s.query(Frame.target_id, Frame.captured_at)
                     .filter(Frame.session_id == session_id)
                     .order_by(Frame.captured_at.desc())
                     .first())
            target_id = row[0] if row else None
            if not target_id:
                return
            from atlas.db.models import Target
            tgt = s.get(Target, target_id)
            if tgt is None or tgt.ra_deg is None or tgt.dec_deg is None:
                return
            target_name = tgt.name
            ra = float(tgt.ra_deg); dec = float(tgt.dec_deg)

        now = _dt.utcnow()
        lat = float(site.latitude); lon = float(site.longitude)
        try:
            moon_ra, moon_dec, illum = moon_position(now)
            moon_alt, _ = compute_alt_az(moon_ra, moon_dec, lat, lon, now)
        except Exception:
            return
        if moon_alt <= 0 or illum is None or illum < 0.30:
            self._clear("moon_close_runtime")
            return
        sep = angular_separation(ra, dec, moon_ra, moon_dec)
        if sep >= 30.0:
            self._clear("moon_close_runtime")
            return

        # Within 30° of a bright moon. Is the operator equipped for it?
        equip = ConfigManager.get_equipment()
        has_nb = False
        try:
            filters = (equip.filters or []) if equip else []
            has_nb = any(f.lower() in ("ha", "halpha", "h-alpha",
                                          "oiii", "o3", "sii", "s2",
                                          "nb")
                          for f in filters)
        except Exception:
            pass
        sev = (AlertSeverity.WARNING if not has_nb
                else AlertSeverity.WARNING)   # always warning, not critical
        await self._raise(
            sev, "moon_close_runtime",
            (f"Moon {illum*100:.0f}% illum, alt {moon_alt:.0f}° — "
              f"{target_name} now {sep:.0f}° from moon"
              + ("; no narrowband filter installed to mitigate" if not has_nb
                  else "; narrowband filter available")),
            session_id=session_id,
            data={"illum_pct": round(illum * 100, 0),
                    "moon_alt_deg": round(moon_alt, 1),
                    "separation_deg": round(sep, 1),
                    "target": target_name,
                    "has_narrowband_filter": has_nb},
        )

    def _resolve_polling_mode(self, snap, t, is_dark: bool,
                                 session_active: bool) -> str:
        """Decide which polling mode the WeatherCache should use.

        BORDERLINE  (60 s)  — any metric within 20% of its critical
                               threshold. Whatever phase we're in, if
                               weather is moving toward critical we
                               want to see it fast.
        ACTIVE      (90 s)  — session running, OR we're inside the
                               astronomical dark window, OR we're in
                               evening / morning twilight (operator
                               may start a session at any moment).
        IDLE        (900 s) — daytime with no session and conditions
                               comfortably inside the safe band.
        """
        from atlas.weather.cache import (
            MODE_IDLE, MODE_ACTIVE, MODE_BORDERLINE,
        )

        # ANY-metric-near-critical wins over phase — storms don't care
        # what time of day it is, and we want to feed the dashboard
        # live data when the operator is watching.
        def near(value: float, crit: float) -> bool:
            try:
                return float(value) >= float(crit) * BORDERLINE_FRACTION
            except (TypeError, ValueError):
                return False
        dm_c = snap.temperature_c - snap.dew_point_c
        if (near(snap.wind_speed_ms, t.wind_speed_critical_ms) or
            near(snap.cloud_cover_pct, t.cloud_cover_critical_pct) or
            near(snap.humidity_pct, t.humidity_critical_pct) or
            dm_c <= float(t.dew_margin_critical_c) * 1.25):
            return MODE_BORDERLINE

        # Session running OR inside / approaching the imaging window?
        # Bump to active polling so the verdict watcher sees changes
        # within ~90 seconds. Sun above horizon AND well clear of
        # any twilight → idle.
        from atlas.astronomy.day_phase import (
            PHASE_DAY, current_phase as _phase,
        )
        # Cheap re-classify from the snapshot's site coords — caller
        # has them but doesn't pass them through. Recompute here.
        try:
            from atlas.db.managers import ConfigManager
            site = ConfigManager.get_site()
            if site is not None:
                p = _phase(float(site.latitude), float(site.longitude))
                in_or_near_dark = p.phase != PHASE_DAY
            else:
                in_or_near_dark = is_dark
        except Exception:
            in_or_near_dark = is_dark

        if session_active or in_or_near_dark:
            return MODE_ACTIVE
        return MODE_IDLE

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

        # Read through WeatherCache. Polling mode is adaptive: the
        # Critic sets it each tick based on session state + verdict +
        # metric trend, and the cache's TTL follows. So an active
        # session running on a borderline night refreshes every 60 s
        # without us issuing manual force_refresh calls.
        from atlas.weather.cache import get_weather_cache, MODE_IDLE
        cache = get_weather_cache()
        # First read uses whatever mode is already set (cold boot = IDLE).
        # We re-set the mode immediately below once we have a snapshot
        # and know whether we're borderline.
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

        # ----------------------------------------------------------------
        # Adaptive polling mode: tighten the cache TTL when conditions
        # warrant. The Critic is the only thing that knows the live
        # combination of (session running, weather, day phase), so it
        # owns the decision.
        # ----------------------------------------------------------------
        try:
            from atlas.db.managers import SessionManager
            from atlas.db.models import SessionState as _SS
            latest_sess = SessionManager.latest()
            session_active = (latest_sess is not None
                                and getattr(latest_sess, "state", None) in
                                    (_SS.NOMINAL, _SS.WARNING))
        except Exception:
            session_active = False
        new_mode = self._resolve_polling_mode(snap, t, is_dark, session_active)
        prev_mode = cache.set_mode(new_mode)
        if prev_mode != new_mode:
            self.log.info(
                "Critic switched polling mode %s → %s (session_active=%s, "
                "is_dark=%s, overall=%s)",
                prev_mode, new_mode, session_active, is_dark, overall,
            )

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
