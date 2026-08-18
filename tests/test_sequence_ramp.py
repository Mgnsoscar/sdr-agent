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


def _base_steps(ramp: RampSpec, anchor="start", offset=0.0, offset_end=None):
    return [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="chirp"),
        SequenceStep(anchor=anchor, offset_s=offset, offset_end_s=offset_end,
                     action=StepAction.RAMP, task_name="chirp", ramp=ramp),
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


def test_both_anchor_ramp_respects_insets(tmp_path):
    async def scenario():
        mgr, runner = _runner(tmp_path)
        await mgr.startup(); await runner.startup()
        try:
            ramp = RampSpec(param="gain", start=0, stop=30, hold_s=10)
            # 60s window, start 5s after on-air, end 5s before off-air ⇒ 50s span.
            seq = await runner.create_sequence(CreateSequenceRequest(
                name="inset", steps=_base_steps(ramp, anchor="both", offset=5.0, offset_end=-5.0)))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=30)).isoformat()),
                (now + timedelta(seconds=90)).isoformat(),   # 60s window
            )
            tunes = sorted((s for s in run.steps if s.action == "tune"), key=lambda s: s.offset_s)
            assert tunes[0].offset_s == pytest.approx(5)     # starts at on-air + 5
            assert tunes[-1].offset_s == pytest.approx(55)   # ends at off-air - 5 (60-5)
        finally:
            await runner.shutdown(); await mgr.shutdown()
    asyncio.run(scenario())


def test_run_mode_ramp_expands_to_run_fires(tmp_path):
    """A run-mode ramp fires the task once per point with the value as a CLI arg
    (fixed args from the step, the ramped param appended as `flag value`)."""
    async def scenario():
        mgr, runner = _runner(tmp_path)
        await mgr.startup(); await runner.startup()
        try:
            ramp = RampSpec(param="atten", start=0, stop=30, step=10, hold_s=1,
                            mode="run", flag="--atten")
            steps = [
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="chirp"),
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.RAMP, task_name="chirp",
                             ramp=ramp, args=["--dwell", "5"]),
                SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="chirp"),
            ]
            seq = await runner.create_sequence(CreateSequenceRequest(name="run-ramp", steps=steps))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=30)).isoformat()),
                (now + timedelta(seconds=90)).isoformat(),
            )
            runs = sorted((s for s in run.steps if s.action == "run"), key=lambda s: s.offset_s)
            assert [s.args for s in runs] == [
                ["--dwell", "5", "--atten", "0"],
                ["--dwell", "5", "--atten", "10"],
                ["--dwell", "5", "--atten", "20"],
                ["--dwell", "5", "--atten", "30"],
            ]
            assert not [s for s in run.steps if s.action == "tune"]
        finally:
            await runner.shutdown(); await mgr.shutdown()
    asyncio.run(scenario())


def test_run_mode_integer_values_have_no_decimal(tmp_path):
    """Integer-typed run ramps render whole numbers (so `type=int` argparse accepts)."""
    async def scenario():
        mgr, runner = _runner(tmp_path)
        await mgr.startup(); await runner.startup()
        try:
            ramp = RampSpec(param="atten", start=0, stop=10, steps=3, hold_s=1,
                            mode="run", flag="-a", integer=True)   # 0, 3.33, 6.67, 10 → rounded
            steps = [
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="chirp"),
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.RAMP, task_name="chirp", ramp=ramp),
                SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="chirp"),
            ]
            seq = await runner.create_sequence(CreateSequenceRequest(name="int-ramp", steps=steps))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=30)).isoformat()),
                (now + timedelta(seconds=90)).isoformat(),
            )
            vals = [s.args[-1] for s in sorted((s for s in run.steps if s.action == "run"),
                                               key=lambda s: s.offset_s)]
            assert vals == ["0", "3", "7", "10"]   # rounded, no ".0"
        finally:
            await runner.shutdown(); await mgr.shutdown()
    asyncio.run(scenario())
