"""
Arm-time inline steps: a plan runs a plan-local copy of a sequence — its own step
timing and parameters — without modifying the stored sequence.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, SequenceStep, StepAction, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner


def _make(tmp: Path):
    (tmp / "tuner.py").write_text(
        "import sys\nprint('TUNER ' + ' '.join(sys.argv[1:]), flush=True)\n")
    tasks = {
        "tuner": TaskConfig(name="tuner",
                            command=["python3", str(tmp / "tuner.py")],
                            working_dir=str(tmp)),
    }
    mgr = ProcessManager(tasks, tmp, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp / "seq.json", tmp / "runs.json", tmp)
    return mgr, runner


async def _arm(tmp: Path, req_steps):
    mgr, runner = _make(tmp)
    await mgr.startup()
    await runner.startup()
    # Stored sequence: a single on-air run of tuner at 100.0.
    seq = await runner.create_sequence(CreateSequenceRequest(name="base", steps=[
        SequenceStep(anchor="start", offset_s=0, action=StepAction.RUN,
                     task_name="tuner", args=["-f", "100.0"], replace_args=True),
        SequenceStep(anchor="stop", offset_s=0, action=StepAction.STOP, task_name="tuner"),
    ]))
    now = datetime.now(timezone.utc)
    run = await runner.arm(
        seq.id,
        ArmSequenceRequest(on_air_at=(now + timedelta(seconds=30)).isoformat(),
                           open_ended=True, steps=req_steps),
        None,
    )
    stored = runner.get_sequence(seq.id)
    await runner.shutdown()
    await mgr.shutdown()
    return run, stored


def test_inline_steps_replace_the_stored_sequence_for_the_run(tmp_path):
    # Plan-local copy: two on-air runs at different times + frequencies.
    plan_steps = [
        SequenceStep(anchor="start", offset_s=-10, action=StepAction.START,
                     task_name="tuner", args=["-f", "107.9"], replace_args=True),
        SequenceStep(anchor="start", offset_s=5, action=StepAction.RUN,
                     task_name="tuner", args=["-f", "103.5"], replace_args=True),
        SequenceStep(anchor="stop", offset_s=0, action=StepAction.STOP, task_name="tuner"),
    ]
    run, stored = asyncio.run(_arm(tmp_path, plan_steps))
    # The run fires the PLAN-LOCAL steps…
    fires = [(s.anchor, s.offset_s, s.action, tuple(s.args)) for s in run.steps]
    assert ("start", -10.0, "start", ("-f", "107.9")) in fires
    assert ("start", 5.0, "run", ("-f", "103.5")) in fires
    # …while the stored sequence is unchanged.
    assert len(stored.steps) == 2
    assert stored.steps[0].args == ["-f", "100.0"]


def test_none_steps_uses_the_stored_sequence(tmp_path):
    run, stored = asyncio.run(_arm(tmp_path, None))
    run_step = next(s for s in run.steps if s.action == "run")
    assert run_step.args == ["-f", "100.0"]


def test_inline_steps_are_validated(tmp_path):
    # No stop step → invalid, like any sequence.
    bad = [SequenceStep(anchor="start", offset_s=0, action=StepAction.RUN,
                        task_name="tuner", args=[], replace_args=True)]
    with pytest.raises(ValueError, match="stop"):
        asyncio.run(_arm(tmp_path, bad))


def test_inline_steps_reject_unknown_task(tmp_path):
    bad = [SequenceStep(anchor="start", offset_s=0, action=StepAction.RUN,
                        task_name="ghost", args=[], replace_args=True),
           SequenceStep(anchor="stop", offset_s=0, action=StepAction.STOP,
                        task_name="ghost")]
    with pytest.raises(ValueError, match="unknown task"):
        asyncio.run(_arm(tmp_path, bad))
