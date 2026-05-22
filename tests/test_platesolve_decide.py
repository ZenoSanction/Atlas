"""Plate-solve decide() truth table."""
from __future__ import annotations

from atlas.capture.platesolve_orchestrator import (
    PlateSolveContext, decide,
)


def test_operator_request_outranks_everything():
    ctx = PlateSolveContext(
        operator_requested=True, is_first_frame_on_target=True,
        after_meridian_flip=True, after_guiding_recovery=True,
    )
    d = decide(ctx)
    assert d.should_solve is True
    assert d.trigger == "operator_request"


def test_first_frame_when_no_operator_request():
    ctx = PlateSolveContext(is_first_frame_on_target=True,
                                target_name="M51")
    d = decide(ctx)
    assert d.should_solve is True
    assert d.trigger == "first_frame"
    assert "M51" in d.reason


def test_meridian_flip_when_not_first_frame():
    ctx = PlateSolveContext(after_meridian_flip=True)
    d = decide(ctx)
    assert d.should_solve is True
    assert d.trigger == "meridian_flip"


def test_guiding_recovery_when_nothing_else():
    ctx = PlateSolveContext(after_guiding_recovery=True)
    d = decide(ctx)
    assert d.should_solve is True
    assert d.trigger == "guiding_recovery"


def test_nothing_triggered_returns_skip():
    d = decide(PlateSolveContext())
    assert d.should_solve is False
    assert d.trigger == "(none)"


def test_priority_order():
    # All four triggers armed — priority is operator > first_frame >
    # meridian_flip > guiding_recovery > (none)
    triggers = [
        (PlateSolveContext(operator_requested=True), "operator_request"),
        (PlateSolveContext(is_first_frame_on_target=True), "first_frame"),
        (PlateSolveContext(after_meridian_flip=True), "meridian_flip"),
        (PlateSolveContext(after_guiding_recovery=True),
            "guiding_recovery"),
    ]
    for ctx, expected in triggers:
        d = decide(ctx)
        assert d.trigger == expected
