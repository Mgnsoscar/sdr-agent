"""
Regression: aborting an open-ended run must stop the task the RUN armed — even when a
per-slot plan edit made the run's steps reference a different task than the stored
sequence. Previously abort derived its stop-list from the stored sequence, so a
plan-edited task kept running after Stop and had to be killed by hand on the unit.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, SequenceStep, StepAction, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner


async def _scenario(tmp: Path) -> bool:
    # A long-running "beacon" (stays up until stopped) and the original "tuner".
    (tmp / "beacon.py").write_text("import time\nwhile True:\n    time.sleep(0.2)\n")
    (tmp / "tuner.py").write_text("import time\nwhile True:\n    time.sleep(0.2)\n")
    tasks = {
        "beacon": TaskConfig(name="beacon", command=["python3", str(tmp / "beacon.py")],
                             working_dir=str(tmp)),
        "tuner": TaskConfig(name="tuner", command=["python3", str(tmp / "tuner.py")],
                            working_dir=str(tmp)),
    }
    mgr = ProcessManager(tasks, tmp, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp / "seq.json", tmp / "runs.json", tmp)
    await mgr.startup()
    await runner.startup()

    # Stored sequence references ONLY tuner.
    seq = await runner.create_sequence(CreateSequenceRequest(name="base", steps=[
        SequenceStep(anchor="start", offset_s=0, action=StepAction.START, task_name="tuner"),
        SequenceStep(anchor="stop", offset_s=0, action=StepAction.STOP, task_name="tuner"),
    ]))

    # Arm OPEN-ENDED with per-slot edited steps that start BEACON instead (a task the
    # stored sequence never mentions) — the "edited the plan to include another task".
    now = datetime.now(timezone.utc)
    run = await runner.arm(seq.id, ArmSequenceRequest(
        on_air_at=(now + timedelta(seconds=1)).isoformat(), open_ended=True,
        steps=[
            SequenceStep(anchor="start", offset_s=0, action=StepAction.START, task_name="beacon"),
            SequenceStep(anchor="stop", offset_s=0, action=StepAction.STOP, task_name="beacon"),
        ]), None)

    try:
        for _ in range(80):                 # wait for on-air → beacon starts
            if mgr.is_running("beacon"):
                break
            await asyncio.sleep(0.1)
        assert mgr.is_running("beacon"), "beacon should be running once on-air"
        assert not mgr.is_running("tuner"), "the stored-sequence task never ran"

        await runner.cancel_or_abort(run.id)   # this is what pressing Stop triggers

        for _ in range(80):
            if not mgr.is_running("beacon"):
                break
            await asyncio.sleep(0.1)
        return not mgr.is_running("beacon")
    finally:
        await runner.shutdown()
        await mgr.shutdown()


def test_abort_stops_a_plan_edited_task_not_in_the_stored_sequence(tmp_path):
    assert asyncio.run(_scenario(tmp_path)) is True
