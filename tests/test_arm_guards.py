"""Arming guards on a single-TX unit:

  A0 — a sequence cannot be armed if one of its tasks is already running (started by
       hand, by a scheduled event, or by another sequence), and
  A  — a sequence cannot be armed if its on-air window overlaps a run already
       armed/running on this unit.

Both would otherwise collide on the unit's single TX channel and surface as a
confusing UHD "device busy" crash at fire time instead of a clean rejection.
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
