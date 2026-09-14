"""A ramp that CROSSES the Hold is PAUSED there (agent 1.27.0, capability sequence-hold-ramp-pause).

Owner decision (docs/sequence-hold-step.md §5.7): a window-A ramp may start before the Hold and end
after it. Its points up to the pause fire in window A, the level reached holds through the pause, and
the remaining points are DEFERRED (SequenceRun.paused_fires) and resume after proceed shifted by the
pause's length. Edit-while-holding re-derives the remainder from the edited window A. A ≤1.26 agent
kept the whole ramp in window A and silently delayed the pause until it finished.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agent import config as cfg
from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, ProceedRequest, RampSpec, SequenceState,
    SequenceStep, StepAction, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner
from test_sequence_hold_runtime import _mk

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _runner(tmp_path):
    tasks = {"tx": TaskConfig(name="tx", command=["python3", "/x/tx.py"], working_dir="/x")}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    return SequenceRunner(mgr, "unit-a", tmp_path / "seq.json", tmp_path / "runs.json", tmp_path)


def _off(fire_iso, base=T0):
    return round((datetime.fromisoformat(fire_iso) - base).total_seconds(), 6)


def _crossing_steps(hold_at=30.0, ramp_at=27.0, stop=9.0):
    """START @0 → RAMP gain 0→stop (steps=3, 6 s: 4 levels × 1.5 s at 27, 28.5, 30, 31.5) → HOLD @30
    → (window B: tune 21 @ resume+0.5) → STOP. The ramp's last point sits PAST the pause."""
    ramp = RampSpec(start=0.0, stop=stop, steps=3, duration_s=6.0, param="gain")
    return [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        SequenceStep(anchor="start", offset_s=ramp_at, action=StepAction.RAMP, task_name="tx",
                     ramp=ramp),
        SequenceStep(anchor="start", offset_s=hold_at, action=StepAction.HOLD, task_name=""),
        SequenceStep(anchor="hold", offset_s=0.5, action=StepAction.TUNE, task_name="tx",
                     params={"gain": 21}),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]


def test_capability_and_version():
    assert "sequence-hold-ramp-pause" in cfg.AGENT_CAPABILITIES
    assert tuple(int(p) for p in cfg.AGENT_VERSION.split(".")) >= (1, 27, 0)


def test_a_crossing_ramp_is_legal_but_a_ramp_starting_after_the_hold_is_not(tmp_path):
    r = _runner(tmp_path)
    r._validate_steps(_crossing_steps())                 # starts before, ends after: fine
    r._validate_steps(_crossing_steps(ramp_at=30.0))     # starting AT the pause: fine too
    with pytest.raises(ValueError, match="after the HOLD"):
        r._validate_steps(_crossing_steps(ramp_at=31.0))


def test_split_fires_at_hold_defers_the_points_past_the_pause(tmp_path):
    r = _runner(tmp_path)
    wa, _wb, hold_off = SequenceRunner._split_hold_windows(_crossing_steps())
    hold_time = T0 + timedelta(seconds=hold_off)
    fires = r._resolve_steps(wa, T0, None, 0.0, True, None, enter_at=hold_time)
    before, after = SequenceRunner._split_fires_at_hold(fires, hold_time)
    pts = sorted((_off(f.fire_at), f.params["gain"]) for f in before if f.action == "tune")
    assert pts == [(27.0, 0.0), (28.5, 3.0), (30.0, 6.0)]     # up to AND including the pause instant
    assert any(f.action == "start" for f in before)            # the launch stays in window A
    assert len(after) == 1
    deferred = after[0]
    assert deferred.anchor == "hold" and deferred.offset_s == 1.5   # 1.5 s AFTER the pause
    assert deferred.params == {"gain": 9.0} and deferred.fired_actual is None
    # nothing to split without a crossing ramp
    wa2, _wb2, _h = SequenceRunner._split_hold_windows(_crossing_steps(ramp_at=20.0))
    fires2 = r._resolve_steps(wa2, T0, None, 0.0, True, None, enter_at=hold_time)
    assert SequenceRunner._split_fires_at_hold(fires2, hold_time)[1] == []


def test_edit_while_holding_re_derives_the_paused_remainder(tmp_path):
    """The operator retargets the crossing ramp's top while holding: the deferred remainder is
    re-derived from the EDITED window A (the part before the pause has already fired)."""
    async def scenario():
        r = _runner(tmp_path)
        seq = await r.create_sequence(CreateSequenceRequest(name="x", steps=_crossing_steps()))
        t0 = datetime.now(timezone.utc) + timedelta(seconds=3600)
        run = await r.arm(seq.id, ArmSequenceRequest(on_air_at=t0.isoformat(), open_ended=True,
                                                     hold_aware=True, max_hold_s=0), None)
        assert [f.params["gain"] for f in run.paused_fires] == [9.0]
        assert all(_off(f.fire_at, t0) <= 30.0 for f in run.steps)     # window A ends at the pause
        # park it (the tick loop isn't running — nothing fires)
        run.state = SequenceState.HOLDING
        run.held_actual = datetime.now(timezone.utc).isoformat()
        edited = _crossing_steps(stop=90.0)                             # a ten-times-higher top
        t_resume = t0 + timedelta(seconds=600)
        out = await r.proceed(run.id, ProceedRequest(proceed_at=t_resume.isoformat(), steps=edited))
        assert out.paused_fires == []                                   # now scheduled
        resumed = [f for f in out.steps if f.anchor == "hold" and f.action == "tune"]
        by_off = sorted((_off(f.fire_at, t_resume), f.params["gain"]) for f in resumed)
        assert by_off == [(0.5, 21), (1.5, 90.0)]           # the edited top, 1.5 s after resume
        assert _off(out.on_air_end, t_resume) == 1.5        # the resumed remainder counts as content
        await r.cancel_or_abort(run.id)

    asyncio.run(scenario())


def test_live_run_pauses_the_ramp_at_the_hold_and_resumes_it_after_proceed(tmp_path, monkeypatch):
    """End-to-end against the live mock task: the ramp reaches 30 when the pause arrives (its 40
    is past the pause), HOLDS 30 through the pause, and reaches 40 after proceed — followed by the
    window-B tune, then off-air."""
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            # ramp 10→40, 4 levels × 0.5 s at 1.0, 1.5, 2.0, 2.5; HOLD @2.2 → 40 is deferred (+0.3)
            ramp = RampSpec(start=10.0, stop=40.0, steps=3, duration_s=2.0, param="gain")
            steps = [
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
                SequenceStep(anchor="start", offset_s=1.0, action=StepAction.RAMP, task_name="tx",
                             ramp=ramp),
                SequenceStep(anchor="start", offset_s=2.2, action=StepAction.HOLD, task_name=""),
                SequenceStep(anchor="hold", offset_s=1.5, action=StepAction.TUNE, task_name="tx",
                             params={"gain": 22}),
                SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
            ]
            seq = await runner.create_sequence(CreateSequenceRequest(name="cross", steps=steps))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=0.4)).isoformat(),
                                   open_ended=True, hold_aware=True, max_hold_s=0),
                None,
            )
            rid = run.id
            assert [(f.offset_s, f.params["gain"]) for f in run.paused_fires] == [(0.3, 40.0)]

            await asyncio.sleep(3.4)              # T0 +0.4 → hold at +2.6 → parked by now
            held = runner.get_run(rid)
            assert held.state == SequenceState.HOLDING
            got = await mgr.get_params("tx")
            assert got["current"]["gain"] == 30   # frozen at the level reached, NOT run through to 40

            t_resume = datetime.now(timezone.utc)
            resumed = await runner.proceed(rid, ProceedRequest(proceed_at=t_resume.isoformat()))
            assert resumed.state == SequenceState.RUNNING and resumed.paused_fires == []
            offs = sorted(round((datetime.fromisoformat(f.fire_at) - t_resume).total_seconds(), 3)
                          for f in resumed.steps if f.anchor == "hold")
            assert offs == [0.3, 1.5]             # the ramp's remainder, then the window-B tune
            await asyncio.sleep(0.9)
            got = await mgr.get_params("tx")
            assert got["current"]["gain"] == 40   # the ramp finished after resume
            await asyncio.sleep(1.6)
            done = runner.get_run(rid)
            assert done.state == SequenceState.COMPLETED
            assert not mgr.is_running("tx")
        finally:
            await runner.shutdown()
            if mgr.is_running("tx"):
                await mgr.stop("tx")
            await mgr.shutdown()

    asyncio.run(scenario())
