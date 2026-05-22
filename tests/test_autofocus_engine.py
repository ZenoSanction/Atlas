"""AutofocusDecisionEngine truth table.

Pure logic — no DB, no hardware. Each test feeds an
AutofocusContext, asserts the (should_fire, trigger) pair the
engine returns. Reference: atlas/capture/autofocus.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from atlas.capture.autofocus import (
    AutofocusContext, AutofocusDecisionEngine,
)


@pytest.fixture
def engine():
    return AutofocusDecisionEngine()


def test_session_start_always_fires(engine):
    ctx = AutofocusContext(session_started=True, is_mono=True)
    d = engine.should_fire(ctx)
    assert d.should_fire is True
    assert d.trigger == "session_start"


def test_session_start_opt_out():
    eng = AutofocusDecisionEngine(trigger_on_session_start=False)
    ctx = AutofocusContext(session_started=True, is_mono=True)
    d = eng.should_fire(ctx)
    # Falls through to the next trigger; with no others armed -> none
    assert d.should_fire is False


def test_filter_change_fires_on_mono(engine):
    ctx = AutofocusContext(filter_changed=True, current_filter="R",
                              is_mono=True)
    d = engine.should_fire(ctx)
    assert d.should_fire is True
    assert d.trigger == "filter_change"
    assert "R" in d.reason


def test_filter_change_skipped_on_osc(engine):
    ctx = AutofocusContext(filter_changed=True, current_filter="OSC",
                              is_mono=False)
    d = engine.should_fire(ctx)
    assert d.should_fire is False
    assert d.trigger == "filter_change"
    assert "OSC" in d.skip_because


def test_temp_delta_fires_at_threshold(engine):
    # Default threshold = 2.0 C
    ctx = AutofocusContext(current_temp_c=-5.0, last_af_temp_c=-2.5,
                              is_mono=True)
    d = engine.should_fire(ctx)
    assert d.should_fire is True
    assert d.trigger == "temp_delta"


def test_temp_delta_does_not_fire_below_threshold(engine):
    ctx = AutofocusContext(current_temp_c=-5.0, last_af_temp_c=-4.0,
                              is_mono=True)
    d = engine.should_fire(ctx)
    # Falls through past temp_delta — no other triggers armed -> none
    assert d.should_fire is False


def test_time_elapsed_fires_at_threshold(engine):
    long_ago = datetime.utcnow() - timedelta(minutes=90)
    ctx = AutofocusContext(last_af_at=long_ago, is_mono=True)
    d = engine.should_fire(ctx)
    assert d.should_fire is True
    assert d.trigger == "time_elapsed"


def test_time_elapsed_does_not_fire_when_recent(engine):
    recent = datetime.utcnow() - timedelta(minutes=10)
    ctx = AutofocusContext(last_af_at=recent, is_mono=True)
    d = engine.should_fire(ctx)
    assert d.should_fire is False


def test_hfr_degradation_fires_at_ratio(engine):
    # Default factor = 1.25
    ctx = AutofocusContext(current_hfr=2.8, reference_hfr=2.0,
                              is_mono=True)
    d = engine.should_fire(ctx)
    assert d.should_fire is True
    assert d.trigger == "hfr_degradation"


def test_hfr_degradation_does_not_fire_at_safe_ratio(engine):
    ctx = AutofocusContext(current_hfr=2.2, reference_hfr=2.0,
                              is_mono=True)
    d = engine.should_fire(ctx)
    assert d.should_fire is False


def test_no_triggers_armed_returns_none(engine):
    d = engine.should_fire(AutofocusContext(is_mono=True))
    assert d.should_fire is False
    assert d.trigger == "(none)"


def test_session_start_outranks_filter_change(engine):
    """Both armed → session_start wins (it comes first in the chain)."""
    ctx = AutofocusContext(session_started=True, filter_changed=True,
                              current_filter="R", is_mono=True)
    d = engine.should_fire(ctx)
    assert d.trigger == "session_start"


def test_custom_temp_threshold():
    eng = AutofocusDecisionEngine(temp_delta_c=5.0)
    # 3°C diff should NOT fire with a 5°C threshold
    ctx = AutofocusContext(current_temp_c=-5.0, last_af_temp_c=-2.0,
                              is_mono=True)
    d = eng.should_fire(ctx)
    assert d.should_fire is False
