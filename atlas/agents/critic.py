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
    if v >= t.wind_speed_critical_ms:
        return MetricCheck("wind", "critical", v, t.wind_speed_critical_ms,
                            f"{v:.1f} m/s ≥ critical {t.wind_speed_critical_ms:.1f}")
    if v >= t.wind_speed_warn_ms:
        return MetricCheck("wind", "warning", v, t.wind_speed_warn_ms,
                            f"{v:.1f} m/s ≥ warn {t.wind_speed_warn_ms:.1f}")
    return MetricCheck("wind", "ok", v, t.wind_speed_warn_ms, "calm")


def _check_dew_margin(snap: WeatherSnapshot, t: SafetyThresholds) -> MetricCheck:
    dm = snap.temperature_c - snap.dew_point_c
    if dm <= t.dew_margin_critical_c:
        return MetricCheck("dew_margin", "critical", dm, t.dew_margin_critical_c,
                            f"{dm:.1f}°C ≤ critical {t.dew_margin_critical_c:.1f}°C")
    if dm <= t.dew_margin_warn_c:
        return MetricCheck("dew_margin", "warning", dm, t.dew_margin_warn_c,
                            f"{dm:.1f}°C ≤ warn {t.dew_margin_warn_c:.1f}°C")
    return MetricCheck("dew_margin", "ok", dm, t.dew_margin_warn_c,
                        f"{dm:.1f}°C — comfortable")


def _check_humidity(snap: WeatherSnapshot, t: SafetyThresholds) -> MetricCheck:
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
    v = snap.precip_mm
    if v >= 0.1:
        return MetricCheck("precip", "critical", v, 0.1,
                            f"{v:.2f} mm in the last hour — close the roof")
    return MetricCheck("precip", "ok", v, 0.0, "dry")


def _hourly_severity(rows: list[dict], t: SafetyThresholds) -> list[dict]:
    """Light per-hour shading for the dashboard — dew margin + cloud cover."""
    out = []
    for r in rows:
        dm = r["temperature_c"] - r["dew_point_c"]
        sev = "ok"
        if dm <= t.dew_margin_critical_c or r["cloud_cover_pct"] >= t.cloud_cover_critical_pct \
                or r["wind_speed_ms"] >= t.wind_speed_critical_ms \
                or r["precip_mm"] >= 0.1:
            sev = "critical"
        elif dm <= t.dew_margin_warn_c or r["cloud_cover_pct"] >= t.cloud_cover_warn_pct \
                or r["wind_speed_ms"] >= t.wind_speed_warn_ms:
            sev = "warning"
        out.append({"time_utc": r["time"], "severity": sev,
                     "dew_margin_c": round(dm, 1),
                     "cloud_cover_pct": round(r["cloud_cover_pct"], 0),
                     "wind_speed_ms": round(r["wind_speed_ms"], 1),
                     "precip_mm": round(r["precip_mm"], 2)})
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
        self._thresholds = SafetyThresholds()
        self._initial_done = False

    async def run(self) -> None:
        self.log.info("Critic agent online (fast %ds, standard %ds)",
                       FAST_LOOP_S, STANDARD_LOOP_S)
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
            await asyncio.sleep(5)

    async def _fast_loop(self) -> None:
        """Fast loop: guiding, focus, frame quality, camera. Imaging-only."""
        sess = SessionManager.latest()
        if sess is None or sess.state.value not in ("nominal", "warning"):
            return
        # TODO Phase 2: pull live values from PHD2 and NINA, emit alerts on
        # threshold breach. For now this is a heartbeat.
        self.log.debug("fast loop tick")

    async def _standard_loop(self) -> None:
        """Standard loop: weather pull + per-metric assessment + push to Operator."""
        site = ConfigManager.get_site()
        if site is None:
            self.log.debug("standard loop: no site config yet, skipping")
            return

        client = OpenMeteoClient(latitude=float(site.latitude),
                                  longitude=float(site.longitude))
        try:
            snap = await client.current()
            forecast_rows = await client.forecast_hours(hours=FORECAST_HOURS)
        except Exception as e:
            self.log.warning("Open-Meteo fetch failed: %s", e)
            return

        t = self._thresholds
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

        assessment = WeatherAssessment(
            observed_at=snap.observed_at,
            assessed_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            overall_severity=overall,
            summary=_summary_from_checks(checks, overall),
            checks=checks,
            raw_current={
                "temperature_c": snap.temperature_c,
                "humidity_pct": snap.humidity_pct,
                "dew_point_c": snap.dew_point_c,
                "dew_margin_c": round(snap.temperature_c - snap.dew_point_c, 1),
                "wind_speed_ms": snap.wind_speed_ms,
                "wind_gust_ms": snap.wind_gust_ms,
                "cloud_cover_pct": snap.cloud_cover_pct,
                "pressure_hpa": snap.pressure_hpa,
                "precip_mm": snap.precip_mm,
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
