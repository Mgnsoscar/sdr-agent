"""A RAMP step expands into tune fires at arm time, and the on-air window is
hard-blocked when it can't fit the sequence's ramps."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, RampSpec, SequenceStep,
    StepAction, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner


def _runner(tmp: Path):
    (tmp / "chirp.py").write_text("import time\nwhile True: time.sleep(1)\n")
    tasks = {"chirp": TaskConfig(name="chirp", command=["python3", str(tmp / "chirp.py")],
                                 working_dir=str(tmp))}
    mgr = ProcessManager(tasks, tmp, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp / "seq.json", tmp / "runs.json", tmp)
    return mgr, runner


def _base_steps(ramp: RampSpec, anchor="start", offset=0.0):
    return [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="chirp"),
        SequenceStep(anchor=anchor, offset_s=offset, action=StepAction.RAMP,
                     task_name="chirp", ramp=ramp),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="chirp"),
    ]


def test_ramp_expands_to_tune_fires(tmp_path):
    async def scenario():
        mgr, runner = _runner(tmp_path)
        await mgr.startup(); await runner.startup()
        try:
            ramp = RampSpec(param="gain", start=0, stop=40, step=10, hold_s=1)  # 0,10,20,30,40
            seq = await runner.create_sequence(CreateSequenceRequest(
                name="ramp-seq", steps=_base_steps(ramp)))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=30)).isoformat()),
                (now + timedelta(seconds=90)).isoformat(),   # 60s window
            )
            tunes = [s for s in run.steps if s.action == "tune"]
            assert len(tunes) == 5
            assert [s.params["gain"] for s in sorted(tunes, key=lambda s: s.offset_s)] == \
                   [0, 10, 20, 30, 40]
            assert [s.offset_s for s in sorted(tunes, key=lambda s: s.offset_s)] == \
                   [0, 1, 2, 3, 4]
        finally:
            await runner.shutdown(); await mgr.shutdown()
    asyncio.run(scenario())


def test_window_too_short_is_hard_blocked(tmp_path):
    async def scenario():
        mgr, runner = _runner(tmp_path)
        await mgr.startup(); await runner.startup()
        try:
            ramp = RampSpec(param="gain", start=0, stop=40, duration_s=120, hold_s=2)
            seq = await runner.create_sequence(CreateSequenceRequest(
                name="long-ramp", steps=_base_steps(ramp)))
            now = datetime.now(timezone.utc)
            with pytest.raises(ValueError, match="at least"):
                await runner.arm(
                    seq.id,
                    ArmSequenceRequest(on_air_at=(now + timedelta(seconds=30)).isoformat()),
                    (now + timedelta(seconds=90)).isoformat(),   # 60s < 120s needed
                )
        finally:
            await runner.shutdown(); await mgr.shutdown()
    asyncio.run(scenario())


def test_both_anchor_ramp_fills_window(tmp_path):
    async def scenario():
        mgr, runner = _runner(tmp_path)
        await mgr.startup(); await runner.startup()
        try:
            ramp = RampSpec(param="gain", start=0, stop=30, hold_s=10)  # step derived from window
            seq = await runner.create_sequence(CreateSequenceRequest(
                name="fill", steps=_base_steps(ramp, anchor="both")))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=30)).isoformat()),
                (now + timedelta(seconds=90)).isoformat(),   # 60s window → 6 intervals
            )
            tunes = sorted((s for s in run.steps if s.action == "tune"),
                           key=lambda s: s.offset_s)
            assert tunes[0].offset_s == 0 and tunes[0].params["gain"] == 0
            assert tunes[-1].offset_s == pytest.approx(60) and tunes[-1].params["gain"] == 30
        finally:
            await runner.shutdown(); await mgr.shutdown()
    asyncio.run(scenario())
