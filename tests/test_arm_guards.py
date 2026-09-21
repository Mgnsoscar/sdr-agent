"""Arming guards on a single-TX unit:

  A0 — a sequence cannot be armed if one of its tasks is already running (started by
       hand, by a scheduled event, or by another sequence), and
  A  — a sequence cannot be armed if its on-air window overlaps a run already
       armed/running on this unit.

Both would otherwise collide on the unit's single TX channel and surface as a
confusing UHD "device busy" crash at fire time instead of a clean rejection.

Since 1.27.3 a whole day of non-overlapping scheduled plans can be armed at once, and a
later one can still be armed while an earlier one is ON AIR: A0 exempts a task that is
running BECAUSE of an active run (its channel span is known, so A decides), and A counts
the stop tail after off-air (the STOP that fires 1 s later) as channel occupation, so two
back-to-back windows collide cleanly at arm instead of at fire time.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, SequenceStep, StepAction,
)
from agent.sequence_runner import SequenceRunner


class FakeManager:
    """Just enough of ProcessManager for the runner's arm-time validation."""
    def __init__(self, tasks):
        self._tasks = set(tasks)
        self.running = set()

    def has_task(self, name):
        return name in self._tasks

    def is_running(self, name):
        return name in self.running

    def get_log_manager(self, name):
        raise KeyError(name)   # run-log opening degrades gracefully


def _runner(tmp_path, tasks):
    mgr = FakeManager(tasks)
    r = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json",
                       tmp_path / "runs.json", tmp_path)
    return mgr, r


def _open_ended_seq(name, task="tx"):
    return CreateSequenceRequest(
        name=name,
        steps=[
            SequenceStep(anchor="start", offset_s=0.0,
                         action=StepAction.START, task_name=task),
            SequenceStep(anchor="stop", offset_s=0.0,
                         action=StepAction.STOP, task_name=task),
        ])


def test_arm_rejected_when_task_already_running(tmp_path):
    async def scenario():
        mgr, r = _runner(tmp_path, ["tx"])
        seq = await r.create_sequence(_open_ended_seq("s1"))
        mgr.running.add("tx")   # task already on air (manual start / another owner)
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="already running"):
            await r.arm(seq.id, ArmSequenceRequest(
                on_air_at=(now + timedelta(seconds=30)).isoformat(),
                open_ended=True), None)
        assert not r.list_runs()   # nothing was armed
    asyncio.run(scenario())


def test_arm_rejected_when_window_overlaps_existing_run(tmp_path):
    async def scenario():
        mgr, r = _runner(tmp_path, ["tx"])
        s1 = await r.create_sequence(_open_ended_seq("s1"))
        s2 = await r.create_sequence(_open_ended_seq("s2"))
        now = datetime.now(timezone.utc)
        # First run: fixed window now+30 .. now+90.
        await r.arm(s1.id, ArmSequenceRequest(
            on_air_at=(now + timedelta(seconds=30)).isoformat(),
            on_air_duration_s=60.0),
            (now + timedelta(seconds=90)).isoformat())
        # Second run overlaps (now+60 .. now+120) → rejected.
        with pytest.raises(ValueError, match="overlaps run"):
            await r.arm(s2.id, ArmSequenceRequest(
                on_air_at=(now + timedelta(seconds=60)).isoformat(),
                on_air_duration_s=60.0),
                (now + timedelta(seconds=120)).isoformat())
        assert len(r.list_runs()) == 1   # only the first armed
    asyncio.run(scenario())


def test_arm_allowed_when_windows_are_disjoint(tmp_path):
    async def scenario():
        mgr, r = _runner(tmp_path, ["tx"])
        s1 = await r.create_sequence(_open_ended_seq("s1"))
        s2 = await r.create_sequence(_open_ended_seq("s2"))
        now = datetime.now(timezone.utc)
        await r.arm(s1.id, ArmSequenceRequest(
            on_air_at=(now + timedelta(seconds=30)).isoformat(),
            on_air_duration_s=60.0),
            (now + timedelta(seconds=90)).isoformat())
        # Second run starts after the first ends (now+120 .. now+180) → allowed.
        await r.arm(s2.id, ArmSequenceRequest(
            on_air_at=(now + timedelta(seconds=120)).isoformat(),
            on_air_duration_s=60.0),
            (now + timedelta(seconds=180)).isoformat())
        assert len(r.list_runs()) == 2
    asyncio.run(scenario())


# ── 1.27.3: arming a whole day of scheduled plans ─────────────────────────────

def _gated_seq(name, task="tx", lead_s=1.0, tail_s=1.0):
    """The shape every client-authored scheduled sequence has: the task launches
    `lead_s` before on-air (muted pre-roll) and stops `tail_s` after off-air."""
    return CreateSequenceRequest(
        name=name,
        steps=[
            SequenceStep(anchor="start", offset_s=-lead_s,
                         action=StepAction.START, task_name=task),
            SequenceStep(anchor="stop", offset_s=tail_s,
                         action=StepAction.STOP, task_name=task),
        ])


async def _arm_window(r, seq, now, start_s, end_s):
    return await r.arm(seq.id, ArmSequenceRequest(
        on_air_at=(now + timedelta(seconds=start_s)).isoformat(),
        on_air_duration_s=float(end_s - start_s)),
        (now + timedelta(seconds=end_s)).isoformat())


def _put_on_air(r, mgr, run):
    """Simulate the run's launch having fired: the task is running BECAUSE of it."""
    run = r._runs[run.id]
    run.state = run.state.__class__.RUNNING
    launch = next(s for s in run.steps if s.action == "start")
    launch.fired_actual = datetime.now(timezone.utc).isoformat()
    mgr.running.add(launch.task_name)


def test_channel_end_is_the_later_of_on_air_end_and_the_last_fire(tmp_path):
    from agent.models import StepFire
    end = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    tail = StepFire(anchor="stop", offset_s=1.0, action="stop", task_name="tx",
                    fire_at=(end + timedelta(seconds=1)).isoformat())
    lead = StepFire(anchor="start", offset_s=-1.0, action="start", task_name="tx",
                    fire_at=(end - timedelta(seconds=61)).isoformat())
    assert SequenceRunner._channel_end(end, [lead, tail]) == end + timedelta(seconds=1)
    assert SequenceRunner._channel_end(end, [lead]) == end          # no tail → on_air_end
    assert SequenceRunner._channel_end(None, [lead, tail]) is None  # open-ended → +∞


def test_later_window_arms_while_an_earlier_run_is_on_air(tmp_path):
    """The owner's case: four plans in the schedule, the first already transmitting —
    arming the next (disjoint) one must not be refused as 'task already running'."""
    async def scenario():
        mgr, r = _runner(tmp_path, ["tx"])
        s1 = await r.create_sequence(_gated_seq("s1"))
        s2 = await r.create_sequence(_gated_seq("s2"))
        now = datetime.now(timezone.utc)
        run1 = await _arm_window(r, s1, now, 30, 90)
        _put_on_air(r, mgr, run1)
        assert mgr.is_running("tx")
        run2 = await _arm_window(r, s2, now, 120, 180)      # disjoint → allowed
        states = {x.id: x.state for x in r.list_runs()}
        assert states[run2.id].value == "armed" and len(states) == 2
    asyncio.run(scenario())


def test_overlapping_window_is_still_refused_while_on_air(tmp_path):
    async def scenario():
        mgr, r = _runner(tmp_path, ["tx"])
        s1 = await r.create_sequence(_gated_seq("s1"))
        s2 = await r.create_sequence(_gated_seq("s2"))
        now = datetime.now(timezone.utc)
        run1 = await _arm_window(r, s1, now, 30, 90)
        _put_on_air(r, mgr, run1)
        with pytest.raises(ValueError, match="overlaps run"):
            await _arm_window(r, s2, now, 60, 120)
        assert len(r.list_runs()) == 1
    asyncio.run(scenario())


def test_a_task_running_outside_any_run_is_still_refused(tmp_path):
    """The exemption is only for a task the ACTIVE run itself launched: a run that is
    merely armed (nothing fired) does not vouch for a hand-started task, and neither
    does a run that already STOPPED the task (a later hand restart is someone else's)."""
    async def scenario():
        mgr, r = _runner(tmp_path, ["tx"])
        s1 = await r.create_sequence(_gated_seq("s1"))
        s2 = await r.create_sequence(_gated_seq("s2"))
        now = datetime.now(timezone.utc)
        run1 = await _arm_window(r, s1, now, 30, 90)
        mgr.running.add("tx")                               # started by hand, run1 only ARMED
        with pytest.raises(ValueError, match="already running"):
            await _arm_window(r, s2, now, 120, 180)
        # …and once run1 has launched AND stopped the task, a running 'tx' is not its own.
        _put_on_air(r, mgr, run1)
        stop = next(s for s in r._runs[run1.id].steps if s.action == "stop")
        stop.fired_actual = datetime.now(timezone.utc).isoformat()
        with pytest.raises(ValueError, match="already running"):
            await _arm_window(r, s2, now, 120, 180)
        assert len(r.list_runs()) == 1
    asyncio.run(scenario())


def test_back_to_back_gated_windows_collide_at_arm_and_need_a_gap(tmp_path):
    """s1 stops its task 1 s AFTER off-air; s2 launches 1 s BEFORE on-air. Windows that
    touch (or leave less than lead-in + tail between them) would have s1's STOP land on
    s2's freshly launched task — refused at arm, with the reason; a wider gap arms."""
    async def scenario():
        mgr, r = _runner(tmp_path, ["tx"])
        s1 = await r.create_sequence(_gated_seq("s1"))
        s2 = await r.create_sequence(_gated_seq("s2"))
        s3 = await r.create_sequence(_gated_seq("s3"))
        now = datetime.now(timezone.utc)
        await _arm_window(r, s1, now, 30, 90)
        with pytest.raises(ValueError, match="overlaps run.*stop tail"):
            await _arm_window(r, s2, now, 90, 150)          # touching
        with pytest.raises(ValueError, match="overlaps run"):
            await _arm_window(r, s2, now, 91, 151)          # 1 s gap: STOP@91 vs START@90
        await _arm_window(r, s3, now, 92, 152)              # lead-in + tail = 2 s gap → ok
        assert len(r.list_runs()) == 2
    asyncio.run(scenario())


def test_a_whole_day_of_disjoint_plans_arms_in_one_go(tmp_path):
    async def scenario():
        mgr, r = _runner(tmp_path, ["tx"])
        seqs = [await r.create_sequence(_gated_seq(f"plan{i}")) for i in range(4)]
        now = datetime.now(timezone.utc)
        for i, seq in enumerate(seqs):                      # 4 disjoint hour-long windows
            await _arm_window(r, seq, now, 60 + i * 3900, 60 + i * 3900 + 3600)
        assert sorted(x.sequence_name for x in r.list_runs()) == [f"plan{i}" for i in range(4)]
    asyncio.run(scenario())


def _oneshot_seq(name, task="atten"):
    """A one-shot RUN of a (non-radio) task + the stop-anchored step every sequence needs."""
    return CreateSequenceRequest(
        name=name,
        steps=[SequenceStep(anchor="start", offset_s=0.0, action=StepAction.RUN, task_name=task),
               SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name=task)])


def _tune_only_seq(name, task="tx"):
    """A sequence that only TUNES a task another run launched (no START / RUN of its own)."""
    return CreateSequenceRequest(
        name=name,
        steps=[SequenceStep(anchor="start", offset_s=5.0, action=StepAction.TUNE, task_name=task,
                            params={"power": "-40"}),
               SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.TUNE, task_name=task,
                            params={"power": "-50"})])


def test_sequences_launching_different_tasks_may_stack(tmp_path):
    """Owner decision (agent 1.36.0, `sequence-stacking`): the overlap guard is TASK-aware — only
    two runs that both LAUNCH the same task while overlapping are refused (a task runs once).
    Runs launching different tasks stack on the unit, and so does a run that only TUNES a task
    another run launched; the owner decides what is compatible."""
    async def scenario():
        mgr, r = _runner(tmp_path, ["tx", "tx2", "atten"])
        s1 = await r.create_sequence(_gated_seq("s1"))                 # launches tx
        s2 = await r.create_sequence(_gated_seq("s2", task="tx2"))     # launches tx2
        s3 = await r.create_sequence(_oneshot_seq("s3"))               # one-shot of a non-radio task
        s4 = await r.create_sequence(_tune_only_seq("s4"))             # only tunes tx
        s5 = await r.create_sequence(_gated_seq("s5"))                 # launches tx again
        now = datetime.now(timezone.utc)
        await _arm_window(r, s1, now, 30, 90)
        await _arm_window(r, s2, now, 60, 120)                         # overlaps s1: different task → stacks
        await _arm_window(r, s3, now, 40, 50)                          # inside s1: different task → stacks
        await _arm_window(r, s4, now, 35, 80)                          # inside s1: tune-only → stacks
        assert len(r.list_runs()) == 4
        with pytest.raises(ValueError, match="overlaps run.*both launch task\\(s\\) 'tx'"):
            await _arm_window(r, s5, now, 60, 120)                     # launches tx while s1 does → refused
        await _arm_window(r, s5, now, 92, 152)                         # after s1's tail → arms
        assert len(r.list_runs()) == 5
    asyncio.run(scenario())
