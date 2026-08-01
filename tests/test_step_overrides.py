"""
Arm-time step overrides: a plan runs a stored sequence with per-task parameters
that differ from its saved definition, without editing the sequence.

Covers:
  * the override replaces the resolved StepFire's args/replace_args (the sequence
    itself is untouched);
  * an out-of-range index and a stop-step target are both rejected;
  * end-to-end, the overridden arg actually reaches the launched process.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, SequenceStep, StepAction,
    StepOverride, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner


def _make(tmp: Path):
    (tmp / "tuner.py").write_text(
        "import sys\nprint('TUNER args: ' + ' '.join(sys.argv[1:]), flush=True)\n")
    tasks = {
        "tuner": TaskConfig(name="tuner",
                            command=["python3", str(tmp / "tuner.py"), "-f", "100.0"],
                            working_dir=str(tmp)),
    }
    mgr = ProcessManager(tasks, tmp, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp / "seq.json", tmp / "runs.json", tmp)
    return mgr, runner


async def _armed_run(tmp: Path, overrides):
    mgr, runner = _make(tmp)
    await mgr.startup()
    await runner.startup()
    seq = await runner.create_sequence(CreateSequenceRequest(name="tune", steps=[
        SequenceStep(anchor="start", offset_s=0, action=StepAction.RUN,
                     task_name="tuner", args=["-f", "100.0"], replace_args=True),
        SequenceStep(anchor="stop", offset_s=0, action=StepAction.STOP, task_name="tuner"),
    ]))
    now = datetime.now(timezone.utc)
    on_air = (now + timedelta(seconds=30)).isoformat()   # far enough out: won't fire
    run = await runner.arm(
        seq.id,
        ArmSequenceRequest(on_air_at=on_air, open_ended=True, step_overrides=overrides),
        None,
    )
    stored = runner.get_sequence(seq.id)   # the sequence must NOT have changed
    await runner.shutdown()
    await mgr.shutdown()
    return run, stored


def test_override_replaces_step_args_without_touching_the_sequence(tmp_path):
    run, stored = asyncio.run(_armed_run(
        tmp_path, [StepOverride(index=0, args=["-f", "107.9"], replace_args=True)]))
    # The resolved fire carries the overridden args…
    run_step = next(s for s in run.steps if s.action == "run")
    assert run_step.args == ["-f", "107.9"]
    assert run_step.replace_args is True
    # …while the stored sequence still holds its original definition.
    assert stored.steps[0].args == ["-f", "100.0"]


def test_no_override_keeps_stored_args(tmp_path):
    run, _ = asyncio.run(_armed_run(tmp_path, []))
    run_step = next(s for s in run.steps if s.action == "run")
    assert run_step.args == ["-f", "100.0"]


def test_out_of_range_index_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="out of range"):
        asyncio.run(_armed_run(tmp_path, [StepOverride(index=9, args=["-f", "1"])]))


def test_override_on_stop_step_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="stop step"):
        asyncio.run(_armed_run(tmp_path, [StepOverride(index=1, args=["-f", "1"])]))


async def _end_to_end(tmp: Path) -> str:
    mgr, runner = _make(tmp)
    await mgr.startup()
    await runner.startup()
    seq = await runner.create_sequence(CreateSequenceRequest(name="tune", steps=[
        SequenceStep(anchor="start", offset_s=0, action=StepAction.RUN,
                     task_name="tuner", args=["-f", "100.0"], replace_args=True),
        SequenceStep(anchor="stop", offset_s=0, action=StepAction.STOP, task_name="tuner"),
    ]))
    now = datetime.now(timezone.utc)
    on_air = (now + timedelta(seconds=1)).isoformat()
    await runner.arm(
        seq.id,
        ArmSequenceRequest(on_air_at=on_air, open_ended=True,
                           step_overrides=[StepOverride(index=0, args=["-f", "107.9"],
                                                        replace_args=True)]),
        None,
    )
    await asyncio.sleep(2.0)   # let the on-air run step fire
    log = mgr.get_log_manager("tuner").current.read_text()
    await runner.shutdown()
    await mgr.shutdown()
    return log


def test_override_reaches_the_launched_process(tmp_path):
    log = asyncio.run(_end_to_end(tmp_path))
    assert "TUNER args: -f 107.9" in log
    assert "100.0" not in log
