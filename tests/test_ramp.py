"""Unit tests for agent.ramp — the parametric ramp expansion + minimum duration."""
import math
from types import SimpleNamespace

import pytest

from agent import ramp


# ── Timing modes ──────────────────────────────────────────────────────────────

def test_step_and_hold_gives_duration():
    # 9 held levels (0..40), each held 2s → duration 18s (every level held, incl. last).
    r = ramp.resolve_ramp(0, 40, step=5, hold_s=2)
    assert r.values == [0, 5, 10, 15, 20, 25, 30, 35, 40]
    assert r.n_intervals == 8
    assert r.hold_s == 2
    assert r.duration_s == 18


def test_step_and_duration_gives_hold():
    # duration is the TOTAL held time spread over all 9 levels → hold = 16/9.
    r = ramp.resolve_ramp(0, 40, step=5, duration_s=16)
    assert r.n_intervals == 8
    assert r.hold_s == pytest.approx(16 / 9)
    assert r.duration_s == pytest.approx(16)


def test_duration_and_hold_gives_level_count():
    # 16s total, 2s per level → 8 held levels spanning 0..40 (7 even intervals).
    r = ramp.resolve_ramp(0, 40, duration_s=16, hold_s=2)
    assert r.n_intervals == 7
    assert len(r.values) == 8
    assert r.values[0] == 0 and r.values[-1] == 40
    assert r.hold_s == 2
    assert r.duration_s == 16


def test_step_not_dividing_clamps_last_to_stop():
    r = ramp.resolve_ramp(0, 42, step=5, hold_s=1)
    assert r.values[0] == 0 and r.values[-1] == 42
    assert r.values[-2] == 40           # honoured step, last point clamped
    assert r.n_intervals == 9


def test_ramp_down():
    r = ramp.resolve_ramp(40, 0, step=10, hold_s=1)
    assert r.values == [40, 30, 20, 10, 0]


def test_dual_anchor_uses_window_for_duration():
    # window 60s, hold 2s → 30 intervals, step derived
    r = ramp.resolve_ramp(0, 30, hold_s=2, window_s=60)
    assert r.duration_s == pytest.approx(60)
    assert r.n_intervals == 30
    assert r.values[0] == 0 and r.values[-1] == 30


def test_underspecified_raises():
    with pytest.raises(ValueError):
        ramp.resolve_ramp(0, 40, step=5)          # no hold/duration
    with pytest.raises(ValueError):
        ramp.resolve_ramp(0, 40, hold_s=2)        # no step/duration
    with pytest.raises(ValueError):
        ramp.resolve_ramp(5, 5, step=1, hold_s=1)  # degenerate


def test_explosion_guarded():
    with pytest.raises(ValueError):
        ramp.resolve_ramp(0, 1e9, step=1, hold_s=1)


# ── include_first / include_last ──────────────────────────────────────────────

def test_exclude_last_drops_stop_level_and_its_hold():
    full = ramp.resolve_ramp(0, 10, step=2, hold_s=1)
    assert full.values == [0, 2, 4, 6, 8, 10] and full.duration_s == 6
    r = ramp.resolve_ramp(0, 10, step=2, hold_s=1, include_last=False)
    assert r.values == [0, 2, 4, 6, 8]          # 10 dropped
    assert r.duration_s == 5                     # one hold shorter


def test_exclude_first_drops_start_level_and_shifts_forward():
    r = ramp.resolve_ramp(0, 10, step=2, hold_s=1, include_first=False)
    assert r.values == [2, 4, 6, 8, 10]
    pts = ramp.place_ramp("start", 0.0, r)
    assert [o for _, o, _ in pts] == [0, 1, 2, 3, 4]   # first emitted starts at offset 0
    assert r.duration_s == 5


def test_two_ramps_chain_without_doubled_seam():
    # A 0→10 ramp excluding its last level, then a 10→20 ramp: 10 appears once and
    # the two durations add, so B starts exactly where A ends.
    a = ramp.resolve_ramp(0, 10, step=2, hold_s=1, include_last=False)
    b = ramp.resolve_ramp(10, 20, step=2, hold_s=1)
    a_pts = ramp.place_ramp("start", 0.0, a)
    b_pts = ramp.place_ramp("start", a.duration_s, b)   # place B after A
    values = [v for _, _, v in a_pts] + [v for _, _, v in b_pts]
    offsets = [o for _, o, _ in a_pts] + [o for _, o, _ in b_pts]
    assert values == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20]   # 10 exactly once
    assert offsets == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]       # contiguous, no gap


def test_exclude_both_empty_raises():
    with pytest.raises(ValueError):
        ramp.resolve_ramp(0, 2, steps=1, hold_s=1,
                          include_first=False, include_last=False)


# ── Placement ─────────────────────────────────────────────────────────────────

def test_place_start_anchored():
    r = ramp.resolve_ramp(0, 20, step=10, hold_s=5)   # values 0,10,20; hold 5
    pts = ramp.place_ramp("start", 0.0, r)
    assert [(a, o) for a, o, _ in pts] == [("start", 0), ("start", 5), ("start", 10)]
    assert [v for _, _, v in pts] == [0, 10, 20]


def test_place_stop_anchored_reserves_final_hold():
    r = ramp.resolve_ramp(20, 0, step=10, hold_s=5)   # values 20,10,0; hold 5
    pts = ramp.place_ramp("stop", 0.0, r)
    # Every level is held; the last (0) is held over the slot ending at the anchor,
    # so it fires one hold (5s) before off-air, not exactly at it.
    assert [(a, o) for a, o, _ in pts] == [("stop", -15), ("stop", -10), ("stop", -5)]
    assert r.duration_s == 15


def test_place_both_fills_from_zero():
    r = ramp.resolve_ramp(0, 20, hold_s=5, window_s=10)   # window 10 → 2 intervals
    pts = ramp.place_ramp("both", 0.0, r)
    assert all(a == "start" for a, _, _ in pts)
    assert pts[0][1] == 0 and pts[-1][1] == pytest.approx(10)


# ── Minimum on-air duration ───────────────────────────────────────────────────

def _step(anchor, offset, action, **kw):
    ramp_obj = None
    if action == "ramp":
        ramp_obj = SimpleNamespace(start=kw["start"], stop=kw["stop"],
                                   step=kw.get("step"), hold_s=kw.get("hold_s"),
                                   duration_s=kw.get("duration_s"))
    return SimpleNamespace(anchor=anchor, offset_s=offset, action=action, ramp=ramp_obj)


def test_min_duration_two_ramps_at_both_ends():
    # 60s ramp-up anchored at on-air start (offset 0) + 60s ramp-down ending at
    # off-air (offset 0) ⇒ minimum on-air 120s.
    steps = [
        _step("start", 0, "start"),                    # start the task
        _step("start", 0, "ramp", start=0, stop=40, duration_s=60, hold_s=2),
        _step("stop", 0, "ramp", start=40, stop=0, duration_s=60, hold_s=2),
        _step("stop", 0, "stop"),
    ]
    assert ramp.min_on_air_duration(steps) == pytest.approx(120)


def test_min_duration_ignores_both_anchor_ramp():
    steps = [
        _step("start", 0, "start"),
        _step("both", 0, "ramp", start=0, stop=40, hold_s=2),  # fills window
        _step("stop", 0, "stop"),
    ]
    assert ramp.min_on_air_duration(steps) == 0.0
