"""
on_air_actual: the run records when it actually crosses T0 (RF live), so a GUI can
show on-air state from the agent's own clock instead of comparing its (possibly
skewed) clock to on_air_at.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, SequenceStep, StepAction, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _run(tmp: Path):
    (tmp / "bc.py").write_text("import time\nprint('live', flush=True)\ntime.sleep(5)\n")
    tasks = {"bc": TaskConfig(name="bc", command=["python3", str(tmp / "bc.py")],
                              working_dir=str(tmp))}
    mgr = ProcessManager(tasks, tmp, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp / "seq.json", tmp / "runs.json", tmp)
    await mgr.startup()
    await runner.startup()

    seq = await runner.create_sequence(CreateSequenceRequest(name="s", steps=[
        SequenceStep(anchor="start", offset_s=0, action=StepAction.START, task_name="bc"),
        SequenceStep(anchor="stop", offset_s=0, action=StepAction.STOP, task_name="bc"),
    ]))
    now = datetime.now(timezone.utc)
    on_air = (now + timedelta(seconds=1)).isoformat()
    run = await runner.arm(seq.id, ArmSequenceRequest(on_air_at=on_air, open_ended=True), None)

    armed_marker = run.on_air_actual        # must still be unset right after arming
    await asyncio.sleep(2.0)                 # let it cross T0
    live_marker = run.on_air_actual          # same object the runner mutates
    on_air_at = run.on_air_at

    await runner.shutdown()
    await mgr.shutdown()
    return armed_marker, live_marker, on_air_at


def test_on_air_actual_is_set_when_t0_is_crossed(tmp_path):
    armed_marker, live_marker, on_air_at = asyncio.run(_run(tmp_path))
    assert armed_marker is None                       # not on air while armed
    assert live_marker is not None                    # set once T0 is crossed
    # …and it lands at or after the scheduled on-air time.
    assert _parse(live_marker) >= _parse(on_air_at) - timedelta(seconds=0.5)
