"""anchor="enter" — a window-A step timed from the Hold's ENTER instant (the pause's start).

The owner's Hold-window ask (v3 #4): a step's END (a ramp finishing as the pause begins) anchors to
the LEFT edge of the Hold window, a step's START / a tune to the resume edge. The agent resolves an
"enter" step at arm from T0 + hold_at_offset_s (known then), in window A, with the stop layout for a
ramp (tied by its end, offset_s <= 0 — nothing may reach INTO the pause). Capability
`sequence-hold-enter` @ AGENT_VERSION 1.26.0 (a ≤1.25 agent rejects the anchor value).
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agent import config as cfg
from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, RampSpec, SequenceStep, StepAction, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner
from test_sequence_hold_runtime import _mk

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _runner(tmp_path):
    tasks = {"tx": TaskConfig(name="tx", command=["python3", "/x/tx.py"], working_dir="/x")}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    return SequenceRunner(mgr, "unit-a", tmp_path / "seq.json", tmp_path / "runs.json", tmp_path)


def _off(fire_iso):
    return round((datetime.fromisoformat(fire_iso) - T0).total_seconds(), 6)


def _base(hold=True, hold_at=30.0):
    steps = [SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx")]
    if hold:
        steps.append(SequenceStep(anchor="start", offset_s=hold_at, action=StepAction.HOLD,
                                  task_name=""))
    steps.append(SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"))
    return steps


def _enter_tune(offset, gain=5):
    return SequenceStep(anchor="enter", offset_s=offset, action=StepAction.TUNE, task_name="tx",
                        params={"gain": gain})


# ── Validation ────────────────────────────────────────────────────────────────

def test_capability_and_version():
    assert "sequence-hold-enter" in cfg.AGENT_CAPABILITIES
    assert tuple(int(p) for p in cfg.AGENT_VERSION.split(".")) >= (1, 26, 0)


def test_validate_enter_requires_a_hold_and_an_offset_at_or_before_the_pause(tmp_path):
    r = _runner(tmp_path)
    r._validate_steps(_base() + [_enter_tune(-5.0)])            # before the pause: fine
    r._validate_steps(_base() + [_enter_tune(0.0)])             # exactly at the pause: fine
    with pytest.raises(ValueError, match="HOLD"):
        r._validate_steps(_base(hold=False) + [_enter_tune(-5.0)])
    with pytest.raises(ValueError, match="at or before the pause"):
        r._validate_steps(_base() + [_enter_tune(2.0)])         # would reach INTO the pause


def test_split_hold_windows_routes_enter_steps_to_window_a():
    steps = _base() + [
        _enter_tune(-5.0),
        SequenceStep(anchor="hold", offset_s=1.0, action=StepAction.TUNE, task_name="tx",
                     params={"gain": 1}),
    ]
    a, b, h = SequenceRunner._split_hold_windows(steps)
    assert h == 30.0
    assert any(s.anchor == "enter" for s in a) and not any(s.anchor == "enter" for s in b)
    assert any(s.anchor == "hold" for s in b) and not any(s.anchor == "hold" for s in a)


def test_lead_offset_counts_an_enter_step_that_precedes_on_air():
    # a Hold 2 s after on-air with a step 5 s before the pause = 3 s BEFORE on-air (a lead-in)
    steps = [SequenceStep(anchor="start", offset_s=2.0, action=StepAction.HOLD, task_name=""),
             _enter_tune(-5.0)]
    assert SequenceRunner._lead_offset(steps) == -3.0
    assert SequenceRunner._lead_offset(_base()) == 0.0


# ── Resolution ────────────────────────────────────────────────────────────────

def test_resolve_places_enter_steps_from_the_pause(tmp_path):
    r = _runner(tmp_path)
    ramp = RampSpec(start=0.0, stop=9.0, steps=3, duration_s=6.0, param="gain")   # 4 levels × 1.5 s
    steps = _base() + [
        _enter_tune(-5.0, gain=77),
        SequenceStep(anchor="enter", offset_s=-2.0, action=StepAction.RAMP, task_name="tx",
                     ramp=ramp),
    ]
    window_a, _b, hold_off = SequenceRunner._split_hold_windows(steps)
    enter_at = T0 + timedelta(seconds=hold_off)
    fires = r._resolve_steps(window_a, T0, None, 0.0, True, None, enter_at=enter_at)
    tune = next(f for f in fires if f.params.get("gain") == 77)
    assert tune.anchor == "enter" and _off(tune.fire_at) == 25.0                  # 30 − 5
    pts = sorted(_off(f.fire_at) for f in fires
                 if f.anchor == "enter" and f.params.get("gain") != 77)
    # a ramp is tied by its END: the last level's hold ends at 30 − 2 = 28 → 22, 23.5, 25, 26.5
    assert pts == [22.0, 23.5, 25.0, 26.5]
    times = [_off(f.fire_at) for f in fires]
    assert times == sorted(times)
    # without the pause instant (not a hold-aware arm) an enter step produces no fire
    none = r._resolve_steps(window_a, T0, None, 0.0, True, None)
    assert not any(f.anchor == "enter" for f in none)


# ── Arm (hold-aware) schedules them in window A ───────────────────────────────

def test_hold_aware_arm_schedules_enter_steps_in_window_a(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            steps = [
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
                SequenceStep(anchor="start", offset_s=5.0, action=StepAction.HOLD, task_name=""),
                _enter_tune(-1.0, gain=44),
                SequenceStep(anchor="hold", offset_s=0.5, action=StepAction.TUNE, task_name="tx",
                             params={"gain": 21}),
                SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
            ]
            seq = await runner.create_sequence(CreateSequenceRequest(name="enter", steps=steps))
            t0 = datetime.now(timezone.utc) + timedelta(seconds=60)      # far enough: nothing fires
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=t0.isoformat(), open_ended=True, hold_aware=True,
                                   max_hold_s=0),
                None,
            )
            enter = [s for s in run.steps if s.anchor == "enter"]
            assert len(enter) == 1 and enter[0].params == {"gain": 44}
            dt = (datetime.fromisoformat(enter[0].fire_at) - t0).total_seconds()
            assert abs(dt - 4.0) < 1e-6                                  # T0 + 5 (hold) − 1
            assert run.hold_at_offset_s == 5.0
            assert not any(s.anchor == "hold" for s in run.steps)         # window B waits for proceed
            await runner.cancel_or_abort(run.id)
        finally:
            await runner.shutdown()
            if mgr.is_running("tx"):
                await mgr.stop("tx")
            await mgr.shutdown()

    asyncio.run(scenario())
