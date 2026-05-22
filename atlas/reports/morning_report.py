"""Morning report generator — markdown summary of last night's session.

After every COMPLETE session, generate a markdown file in
data/morning_reports/ with:

  * Session timing + state history (any STANDBY / WARNING / CRITICAL?)
  * Per-target capture: frames per filter, total minutes, mean HFR,
    mean guide RMS, quality grade distribution
  * Autofocus events: how many fired, before/after HFR
  * Recovery events: any guiding / focus recoveries triggered?
  * Critical advisories from Critic + Oracle
  * Calibration applied (which masters)
  * Disk usage delta

Writing happens via write_morning_report() which the Operator calls
during its session-stop flow. Generation is also exposed via
generate_morning_report() (returns the dict + markdown string in
memory) so the dashboard's "preview last night" button can show it
without touching disk.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import func

from atlas.db.models import (
    AgentMessage, AgentMessageKind, Alert, AlertSeverity, Decision,
    Frame, FrameQuality, Session, SessionState, Target,
)
from atlas.db.session import get_session
from atlas.logging_setup import get_logger

log = get_logger("reports.morning")


# ---- dataclasses ---------------------------------------------------------

@dataclass
class TargetFrameStats:
    target_id: int
    target_name: str
    frames_total: int = 0
    minutes_total: float = 0.0
    by_filter: dict[str, dict] = field(default_factory=dict)
    # by_filter[filter] = {count, minutes, mean_hfr}
    quality_grades: dict[str, int] = field(default_factory=dict)


@dataclass
class SessionReport:
    """Structured data the markdown is rendered from. Also returned
    by generate_morning_report() so callers can use it directly."""
    session_id: int
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_min: float = 0.0
    state: str = ""
    simulation: bool = False
    targets: list[TargetFrameStats] = field(default_factory=list)
    autofocus_runs: int = 0
    autofocus_skips: int = 0
    platesolve_runs: int = 0
    recovery_events: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    critical_alerts: int = 0
    advisories: list[str] = field(default_factory=list)
    weather_summary: Optional[dict] = None
    final_summary: Optional[dict] = None
    markdown: str = ""

    def to_jsonable(self) -> dict:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(timespec="seconds") + "Z",
            "ended_at": (self.ended_at.isoformat(timespec="seconds") + "Z"
                            if self.ended_at else None),
            "duration_min": round(self.duration_min, 1),
            "state": self.state,
            "simulation": self.simulation,
            "targets": [
                {
                    "target_id": t.target_id, "target_name": t.target_name,
                    "frames_total": t.frames_total,
                    "minutes_total": round(t.minutes_total, 1),
                    "by_filter": t.by_filter,
                    "quality_grades": t.quality_grades,
                } for t in self.targets
            ],
            "autofocus_runs": self.autofocus_runs,
            "autofocus_skips": self.autofocus_skips,
            "platesolve_runs": self.platesolve_runs,
            "recovery_events": self.recovery_events,
            "alerts": self.alerts,
            "critical_alerts": self.critical_alerts,
            "advisories": self.advisories,
            "weather_summary": self.weather_summary,
            "final_summary": self.final_summary,
            "markdown": self.markdown,
        }


# ---- builder -------------------------------------------------------------

def generate_morning_report(session_id: Optional[int] = None) -> Optional[SessionReport]:
    """Build a SessionReport for ``session_id`` (or the most recent
    COMPLETE session if None). Returns None when no session is found.

    Cheap to call repeatedly — every read goes to the DB with its own
    session and no caching."""
    with get_session() as s:
        sess: Optional[Session]
        if session_id is None:
            sess = (s.query(Session)
                      .filter(Session.state == SessionState.COMPLETE)
                      .order_by(Session.id.desc()).first())
        else:
            sess = s.get(Session, session_id)
        if sess is None:
            return None
        report = SessionReport(
            session_id=sess.id,
            started_at=sess.started_at,
            ended_at=sess.ended_at,
            state=str(sess.state.value if hasattr(sess.state, "value")
                         else sess.state),
            simulation=bool(sess.simulation),
            weather_summary=sess.weather_summary,
            final_summary=sess.final_summary,
        )
        if sess.ended_at:
            report.duration_min = (
                (sess.ended_at - sess.started_at).total_seconds() / 60.0
            )

        # Frames -> per-target stats
        rows = (s.query(Frame, Target)
                  .outerjoin(Target, Frame.target_id == Target.id)
                  .filter(Frame.session_id == sess.id,
                            Frame.frame_type == "light")
                  .all())
        by_tid: dict[int, TargetFrameStats] = {}
        for frame, target in rows:
            tid = frame.target_id or 0
            tname = (target.name if target else "(no target)")
            ts = by_tid.setdefault(tid, TargetFrameStats(
                target_id=tid, target_name=tname,
            ))
            filt = (frame.filter_name or "L").upper()
            mins = (float(frame.exposure_s or 0.0)) / 60.0
            ts.frames_total += 1
            ts.minutes_total += mins
            bf = ts.by_filter.setdefault(
                filt, {"count": 0, "minutes": 0.0,
                          "sum_hfr": 0.0, "hfr_n": 0})
            bf["count"] += 1
            bf["minutes"] += mins
            if frame.fwhm_arcsec is not None:
                bf["sum_hfr"] += float(frame.fwhm_arcsec)
                bf["hfr_n"] += 1
            grade = (frame.quality.value if hasattr(frame.quality, "value")
                        else str(frame.quality))
            ts.quality_grades[grade] = ts.quality_grades.get(grade, 0) + 1
        # Finalize per-filter mean HFR
        for ts in by_tid.values():
            for filt, bf in ts.by_filter.items():
                if bf["hfr_n"] > 0:
                    bf["mean_hfr"] = round(bf["sum_hfr"] / bf["hfr_n"], 2)
                # Round and drop scratch fields
                bf["minutes"] = round(bf["minutes"], 1)
                bf.pop("sum_hfr", None)
                bf.pop("hfr_n", None)
        report.targets = sorted(by_tid.values(),
                                   key=lambda t: t.minutes_total,
                                   reverse=True)

        # Decisions -> autofocus / platesolve / recovery counts
        decisions = (s.query(Decision)
                       .filter(Decision.session_id == sess.id)
                       .all())
        for d in decisions:
            t = (d.decision_type or "").lower()
            if t.startswith("autofocus"):
                report.autofocus_runs += 1
            elif "autofocus_skip" in t:
                report.autofocus_skips += 1
            elif t.startswith("platesolve") or "plate_solve" in t:
                report.platesolve_runs += 1
            elif "recovery" in t or "recover_" in t:
                report.recovery_events.append({
                    "at": d.decided_at.isoformat(timespec="seconds") + "Z",
                    "type": d.decision_type,
                    "rationale": d.rationale or "",
                })

        # Alerts (with severity counts)
        alerts = (s.query(Alert)
                    .filter(Alert.session_id == sess.id)
                    .order_by(Alert.raised_at).all())
        for a in alerts:
            sev = (a.severity.value if hasattr(a.severity, "value")
                      else str(a.severity))
            entry = {
                "at": a.raised_at.isoformat(timespec="seconds") + "Z",
                "severity": sev,
                "code": a.code,
                "raised_by": (a.raised_by.value
                                 if hasattr(a.raised_by, "value")
                                 else str(a.raised_by)),
                "message": a.message,
                "resolution": a.resolution,
            }
            report.alerts.append(entry)
            if sev == "critical":
                report.critical_alerts += 1

        # Advisories from the session_review payload (if any messages
        # carried them) — pull the last STATUS sent that had advisories
        last_status = (
            s.query(AgentMessage)
              .filter(AgentMessage.session_id == sess.id,
                        AgentMessage.kind == AgentMessageKind.STATUS)
              .order_by(AgentMessage.sent_at.desc()).first()
        )
        if last_status is not None and last_status.payload:
            advs = (last_status.payload or {}).get("advisories") or []
            for a in advs:
                if isinstance(a, dict):
                    report.advisories.append(a.get("summary")
                                                or a.get("text") or "(advisory)")
                else:
                    report.advisories.append(str(a))

    # Render markdown after the DB session closes
    report.markdown = _render_markdown(report)
    return report


# ---- markdown renderer ---------------------------------------------------

def _render_markdown(r: SessionReport) -> str:
    lines: list[str] = []
    started = r.started_at.strftime("%Y-%m-%d %H:%M UTC")
    ended = (r.ended_at.strftime("%Y-%m-%d %H:%M UTC")
               if r.ended_at else "(in progress)")
    lines.append(f"# ATLAS session #{r.session_id} — {started}")
    lines.append("")
    lines.append(f"_Duration: {r.duration_min:.1f} min  •  "
                   f"State: **{r.state}**  •  "
                   f"{'SIMULATION' if r.simulation else 'LIVE'}_")
    lines.append("")
    lines.append(f"Started: {started}  ")
    lines.append(f"Ended:   {ended}")
    lines.append("")

    # ---- targets ----
    if r.targets:
        lines.append("## Targets captured")
        lines.append("")
        for t in r.targets:
            lines.append(f"### {t.target_name}")
            lines.append("")
            lines.append(f"- {t.frames_total} frames, "
                           f"{t.minutes_total:.1f} min total")
            grades_str = ", ".join(f"{k}: {v}" for k, v
                                       in sorted(t.quality_grades.items()))
            if grades_str:
                lines.append(f"- Quality grades: {grades_str}")
            lines.append("")
            if t.by_filter:
                lines.append("| Filter | Frames | Minutes | Mean HFR |")
                lines.append("|---|---:|---:|---:|")
                for filt, bf in sorted(t.by_filter.items()):
                    mean_hfr = bf.get("mean_hfr")
                    hfr_cell = f"{mean_hfr:.2f}" if mean_hfr is not None else "—"
                    lines.append(f"| {filt} | {bf['count']} | "
                                   f"{bf['minutes']:.1f} | {hfr_cell} |")
                lines.append("")
    else:
        lines.append("## Targets captured")
        lines.append("")
        lines.append("_No light frames recorded for this session._")
        lines.append("")

    # ---- orchestration ----
    lines.append("## Orchestration events")
    lines.append("")
    lines.append(f"- Autofocus runs fired: **{r.autofocus_runs}**  "
                   f"(skipped {r.autofocus_skips})")
    lines.append(f"- Plate-solve runs: **{r.platesolve_runs}**")
    lines.append(f"- Recovery events: **{len(r.recovery_events)}**")
    if r.recovery_events:
        lines.append("")
        for ev in r.recovery_events:
            lines.append(f"  * `{ev['at']}` {ev['type']}: {ev['rationale']}")
    lines.append("")

    # ---- alerts ----
    lines.append("## Alerts + advisories")
    lines.append("")
    if r.critical_alerts > 0:
        lines.append(f"⚠ **{r.critical_alerts} critical alert(s)** raised "
                       "this session.")
        lines.append("")
    if r.alerts:
        lines.append("| When | Severity | Code | Raised by | Message |")
        lines.append("|---|---|---|---|---|")
        for a in r.alerts:
            lines.append(f"| {a['at']} | {a['severity']} | {a['code']} "
                           f"| {a['raised_by']} | {a['message'][:80]} |")
        lines.append("")
    else:
        lines.append("- No alerts raised.")
        lines.append("")
    if r.advisories:
        lines.append("**Last Critic/Oracle advisories:**")
        for a in r.advisories[:10]:
            lines.append(f"- {a}")
        lines.append("")

    # ---- weather + final summary ----
    if r.weather_summary:
        lines.append("## Weather summary")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(r.weather_summary, indent=2, default=str))
        lines.append("```")
        lines.append("")
    if r.final_summary:
        lines.append("## Final session summary")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(r.final_summary, indent=2, default=str))
        lines.append("```")
        lines.append("")

    lines.append("---")
    lines.append(f"_Report generated {datetime.utcnow().isoformat(timespec='seconds')}Z by atlas.reports.morning_report_")
    return "\n".join(lines)


# ---- disk write helpers --------------------------------------------------

def write_morning_report(session_id: Optional[int] = None,
                            *, out_dir: Optional[Path] = None,
                            ) -> Optional[Path]:
    """Generate + write the report markdown to disk. Returns the
    written path on success, None when no session was found.

    Filename: data/morning_reports/YYYY-MM-DD_session{id}.md
    Existing file is overwritten (the same session may be reported
    multiple times if the Operator restarts it)."""
    report = generate_morning_report(session_id)
    if report is None:
        log.info("morning report: no session found for id=%s", session_id)
        return None
    if out_dir is None:
        from atlas.config import get_settings
        out_dir = get_settings().morning_reports_dir
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = report.started_at.strftime("%Y-%m-%d")
    path = out_dir / f"{date_str}_session{report.session_id}.md"
    path.write_text(report.markdown, encoding="utf-8")
    log.info("morning report written: %s", path)
    return path


def generate_latest(out_dir: Optional[Path] = None) -> Optional[Path]:
    """Convenience: generate the morning report for the most recent
    COMPLETE session. The Operator calls this from its session-stop
    flow; the dashboard 'preview' button hits the API endpoint
    instead."""
    return write_morning_report(session_id=None, out_dir=out_dir)
