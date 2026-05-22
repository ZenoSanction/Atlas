"""Smoke test for the morning report generator.

Builds a synthetic COMPLETE session with light frames, decisions
(autofocus / platesolve / recovery), alerts, and renders the
markdown. Then verifies key fields landed in the output.

Run from project root:
    venv\\Scripts\\python.exe scripts\\smoke_morning_report.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def _clean_db() -> None:
    from atlas.db.models import (
        AgentMessage, Alert, Decision, Frame, Session, Target,
    )
    from atlas.db.session import get_session
    with get_session() as s:
        s.query(AgentMessage).delete()
        s.query(Alert).delete()
        s.query(Decision).delete()
        s.query(Frame).delete()
        s.query(Target).delete()
        s.query(Session).delete()


def _seed_session(state="complete") -> int:
    from atlas.db.models import Session, SessionState
    from atlas.db.session import get_session
    with get_session() as s:
        sess = Session(
            started_at=datetime.utcnow() - timedelta(hours=4),
            ended_at=datetime.utcnow() - timedelta(minutes=10),
            state=SessionState.COMPLETE if state == "complete" else SessionState.NOMINAL,
            simulation=True,
            weather_summary={"clouds": 5, "humidity": 60},
            final_summary={"frames_total": 30, "mean_hfr": 2.1},
        )
        s.add(sess)
        s.flush()
        return sess.id


def _seed_target(name="M51") -> int:
    from atlas.db.models import Target
    from atlas.db.session import get_session
    with get_session() as s:
        t = Target(name=name, object_type="galaxy",
                    ra_deg=202.47, dec_deg=47.19)
        s.add(t)
        s.flush()
        return t.id


def _seed_frame(session_id, target_id, filter_name, exposure_s,
                  fwhm=2.0, quality_str="B"):
    from atlas.db.models import Frame, FrameQuality
    from atlas.db.session import get_session
    with get_session() as s:
        f = Frame(
            session_id=session_id, target_id=target_id,
            captured_at=datetime.utcnow() - timedelta(hours=2),
            file_path=f"/fake/{filter_name}.fit",
            frame_type="light", filter_name=filter_name,
            exposure_s=exposure_s, fwhm_arcsec=fwhm,
            quality=FrameQuality(quality_str),
        )
        s.add(f)


def _seed_decision(session_id, agent_str, decision_type, rationale=""):
    from atlas.db.models import AgentName, Decision
    from atlas.db.session import get_session
    with get_session() as s:
        d = Decision(
            session_id=session_id,
            decided_at=datetime.utcnow() - timedelta(hours=1),
            agent=AgentName(agent_str),
            decision_type=decision_type,
            rationale=rationale,
        )
        s.add(d)


def _seed_alert(session_id, severity_str, code, message, agent_str="critic"):
    from atlas.db.models import AgentName, Alert, AlertSeverity
    from atlas.db.session import get_session
    with get_session() as s:
        a = Alert(
            session_id=session_id,
            raised_at=datetime.utcnow() - timedelta(hours=1),
            severity=AlertSeverity(severity_str),
            code=code, message=message,
            raised_by=AgentName(agent_str),
        )
        s.add(a)


def test_generation() -> None:
    _hr("1. generate_morning_report builds structured + markdown")
    _clean_db()
    sid = _seed_session()
    tid = _seed_target("M51")
    # 10x L 180s + 5x R 120s
    for _ in range(10):
        _seed_frame(sid, tid, "L", 180, fwhm=2.1, quality_str="B")
    for _ in range(5):
        _seed_frame(sid, tid, "R", 120, fwhm=2.3, quality_str="A")
    # Decisions
    _seed_decision(sid, "operator", "autofocus_fire", "session start")
    _seed_decision(sid, "operator", "autofocus_fire", "filter change")
    _seed_decision(sid, "operator", "platesolve_complete", "first frame OK")
    _seed_decision(sid, "operator", "recovery_guiding_recovered",
                       "restart_guide succeeded")
    # Alerts
    _seed_alert(sid, "warning", "dew_risk", "humidity near 75%")
    _seed_alert(sid, "critical", "guiding_lost",
                  "guiding RMS spike > 3.0 px for 5 min", "critic")

    from atlas.reports.morning_report import generate_morning_report
    report = generate_morning_report(sid)
    assert report is not None
    print(f"  session_id={report.session_id}  duration={report.duration_min:.1f} min")
    print(f"  targets: {[t.target_name for t in report.targets]}")
    print(f"  autofocus_runs={report.autofocus_runs}  "
          f"platesolve_runs={report.platesolve_runs}  "
          f"recoveries={len(report.recovery_events)}")
    print(f"  alerts={len(report.alerts)}  critical={report.critical_alerts}")
    assert report.session_id == sid
    assert len(report.targets) == 1
    m51 = report.targets[0]
    assert m51.frames_total == 15
    assert "L" in m51.by_filter and "R" in m51.by_filter
    assert m51.by_filter["L"]["count"] == 10
    assert m51.by_filter["L"]["mean_hfr"] == 2.1
    assert m51.by_filter["R"]["mean_hfr"] == 2.3
    assert report.autofocus_runs == 2
    assert report.platesolve_runs == 1
    assert len(report.recovery_events) == 1
    assert report.critical_alerts == 1


def test_markdown_renders() -> None:
    _hr("2. markdown contains expected sections")
    from atlas.reports.morning_report import generate_morning_report
    report = generate_morning_report()
    assert report is not None
    md = report.markdown
    print(f"  markdown length: {len(md)} chars")
    assert "# ATLAS session" in md
    assert "## Targets captured" in md
    assert "## Orchestration events" in md
    assert "## Alerts + advisories" in md
    assert "M51" in md
    assert "| L |" in md and "| R |" in md
    print(f"  first 6 lines:")
    for line in md.splitlines()[:6]:
        print(f"    {line}")


def test_disk_write() -> None:
    _hr("3. write_morning_report drops a .md file in target dir")
    from atlas.reports.morning_report import write_morning_report
    with tempfile.TemporaryDirectory() as td:
        path = write_morning_report(out_dir=Path(td))
        assert path is not None
        print(f"  wrote: {path}")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert len(content) > 200
        assert "ATLAS session" in content


def test_no_session_returns_none() -> None:
    _hr("4. no session in DB -> None (no crash)")
    _clean_db()
    from atlas.reports.morning_report import generate_morning_report
    r = generate_morning_report()
    print(f"  result: {r}")
    assert r is None


def main() -> None:
    test_generation()
    test_markdown_renders()
    test_disk_write()
    test_no_session_returns_none()
    _hr("ALL SMOKE TESTS PASSED")
    _clean_db()
    print("  (test rows cleaned)")


if __name__ == "__main__":
    main()
