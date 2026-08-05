"""A sequence's scheduled TUNE step retunes a running duration task's live
parameters at its offset — end to end through the SequenceRunner."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import process_manager as pm
from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, SequenceStep, StepAction, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner

REPO_ROOT = str(Path(__file__).resolve().parents[1])

LIVE_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("chirp")
     .integer("--gain", min=0, max=49, default=30, live=True)
     .number("--freq", min=1, max=1e12, default=100.0, live=True))
args = s.parse()
ctrl = s.live_control(args)
gain, freq = args.gain, args.freq
while True:
    for ch in ctrl.drain():
        if ch.name == "gain":
            gain = (int(ch.value) // 2) * 2      # device steps in even dB
            ctrl.report("gain", gain)
        else:
            freq = ch.value
            ctrl.report("freq", freq)
    time.sleep(0.01)
'''


def test_scheduled_tune_step_applies_live(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")

    script = tmp_path / "chirp.py"
    script.write_text(LIVE_SCRIPT)
    tasks = {
        "chirp": TaskConfig(name="chirp",
                            command=["python3", str(script)],
                            working_dir=str(tmp_path),
                            env={"PYTHONPATH": REPO_ROOT}),
    }

    async def scenario():
        mgr = ProcessManager(tasks, tmp_path, "unit-a")
        runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json",
                                tmp_path / "runs.json", tmp_path)
        await mgr.startup()
        await runner.startup()
        try:
            seq = await runner.create_sequence(CreateSequenceRequest(
                name="chirp-run",
                steps=[
                    # T0: start the duration task.
                    SequenceStep(anchor="start", offset_s=0.0,
                                 action=StepAction.START, task_name="chirp"),
                    # T0+1s: retune its gain live.
                    SequenceStep(anchor="start", offset_s=1.0,
                                 action=StepAction.TUNE, task_name="chirp",
                                 params={"gain": 41}),
                    # (skipped while open-ended, but needed to define the window)
                    SequenceStep(anchor="stop", offset_s=0.0,
                                 action=StepAction.STOP, task_name="chirp"),
                ]))
            now = datetime.now(timezone.utc)
            await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=0.5)).isoformat(),
                                   open_ended=True),
                None,
            )

            # Wait for the start step (≈+0.5s) then the tune step (≈+1.5s).
            await asyncio.sleep(3.0)

            got = await mgr.get_params("chirp")
            assert got["current"]["gain"] == 41       # requested by the tune step
            assert got["applied"]["gain"] == 40        # device quantised 41 → 40
        finally:
            await runner.shutdown()
            await mgr.stop("chirp")
            await mgr.shutdown()

    asyncio.run(scenario())
