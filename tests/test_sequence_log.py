"""
End-to-end sequence run logging: arm and run a real sequence, then assert the
per-run log holds both the annotated choreography AND each step's program output
(collected from the tasks' own logs).
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


def _write_scripts(tmp: Path):
    (tmp / "broadcast.py").write_text(
        "import time\nprint('BROADCAST tuned 100.0MHz', flush=True)\ntime.sleep(3)\n")
    (tmp / "atten.py").write_text(
        "print('ATTEN set to 20 dB', flush=True)\n")


async def _run(tmp: Path) -> str:
    _write_scripts(tmp)
    tasks = {
        "broadcast": TaskConfig(name="broadcast",
                                command=["python3", str(tmp / "broadcast.py")],
                                working_dir=str(tmp)),
        "atten": TaskConfig(name="atten",
                            command=["python3", str(tmp / "atten.py")],
                            working_dir=str(tmp)),
    }
    mgr = ProcessManager(tasks, tmp, "unit-a")
    await mgr.startup()
    runner = SequenceRunner(mgr, "unit-a", tmp / "seq.json", tmp / "runs.json", tmp)
    await runner.startup()

    seq = await runner.create_sequence(CreateSequenceRequest(name="morning", steps=[
        SequenceStep(anchor="start", offset_s=0, action=StepAction.START, task_name="broadcast"),
        SequenceStep(anchor="start", offset_s=0, action=StepAction.RUN, task_name="atten"),
        SequenceStep(anchor="stop", offset_s=0.5, action=StepAction.STOP, task_name="broadcast"),
    ]))

    now = datetime.now(timezone.utc)
    on_air = (now + timedelta(seconds=1)).isoformat()
    on_air_end = (now + timedelta(seconds=2)).isoformat()
    await runner.arm(seq.id, ArmSequenceRequest(on_air_at=on_air, on_air_end=on_air_end),
                     on_air_end)

    await asyncio.sleep(3.5)   # let it fire on-air → off-air → complete

    log = "\n".join(await runner.get_sequence_log_manager(seq.id).tail(1000))
    await runner.shutdown()
    await mgr.shutdown()
    return log


def test_sequence_run_log_has_timeline_and_output(tmp_path):
    log = asyncio.run(_run(tmp_path))
    # ── annotated choreography ──
    assert "sequence 'morning'" in log            # header
    assert "armed" in log
    assert "ON AIR" in log
    assert "▶ start broadcast" in log
    assert "⚡ run atten" in log
    assert "⏹ stop broadcast" in log
    assert "OFF AIR" in log
    assert "completed" in log
    # ── interleaved program output, prefixed by task name ──
    assert "broadcast: BROADCAST tuned 100.0MHz" in log
    assert "atten: ATTEN set to 20 dB" in log
    # ── the one-shot marker header (and its blank line) are NOT in the run log ──
    assert "one-shot:" not in log
    assert "atten: \n" not in log and not log.endswith("atten: ")
    # ordering: the start annotation comes before its output
    assert log.index("▶ start broadcast") < log.index("broadcast: BROADCAST tuned 100.0MHz")
