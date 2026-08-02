"""
Library deploy: SequenceRunner.apply_sequences converges a unit's sequences to a
supplied set, PRESERVING their ids (so every unit shares the id a plan references)
and never removing a sequence with an active run.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, Sequence, SequenceStep, StepAction,
    TaskConfig,
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


def _seq(seq_id: str, name: str) -> Sequence:
    return Sequence(id=seq_id, name=name, steps=[
        SequenceStep(anchor="start", offset_s=0, action=StepAction.START, task_name="tuner"),
        SequenceStep(anchor="stop", offset_s=0, action=StepAction.STOP, task_name="tuner"),
    ])


async def _run(tmp: Path, coro):
    mgr, runner = _make(tmp)
    await mgr.startup()
    await runner.startup()
    try:
        return await coro(runner)
    finally:
        await runner.shutdown()
        await mgr.shutdown()


def test_apply_preserves_ids(tmp_path):
    async def go(runner):
        up, deleted, skipped = await runner.apply_sequences(
            [_seq("seq_fixed1", "alpha"), _seq("seq_fixed2", "beta")], prune=True)
        assert set(up) == {"seq_fixed1", "seq_fixed2"}
        # The stored ids are exactly the library's ids — not freshly minted.
        ids = {s.id for s in runner.list_sequences()}
        assert ids == {"seq_fixed1", "seq_fixed2"}
        assert runner.get_sequence("seq_fixed1").name == "alpha"
        return deleted, skipped
    deleted, skipped = asyncio.run(_run(tmp_path, go))
    assert deleted == [] and skipped == []


def test_prune_removes_omitted_sequences(tmp_path):
    async def go(runner):
        await runner.apply_sequences([_seq("a", "a"), _seq("b", "b")], prune=True)
        # Redeploy a library that omits "b" → b is pruned.
        up, deleted, skipped = await runner.apply_sequences([_seq("a", "a")], prune=True)
        assert up == ["a"] and deleted == ["b"] and skipped == []
        assert {s.id for s in runner.list_sequences()} == {"a"}
    asyncio.run(_run(tmp_path, go))


def test_no_prune_keeps_omitted_sequences(tmp_path):
    async def go(runner):
        await runner.apply_sequences([_seq("a", "a"), _seq("b", "b")], prune=True)
        up, deleted, skipped = await runner.apply_sequences([_seq("a", "a2")], prune=False)
        assert deleted == [] and skipped == []
        assert {s.id for s in runner.list_sequences()} == {"a", "b"}
        assert runner.get_sequence("a").name == "a2"   # still upserted
    asyncio.run(_run(tmp_path, go))


def test_prune_never_deletes_a_sequence_with_an_active_run(tmp_path):
    async def go(runner):
        await runner.apply_sequences([_seq("live", "live")], prune=True)
        now = datetime.now(timezone.utc)
        await runner.arm("live", ArmSequenceRequest(
            on_air_at=(now + timedelta(seconds=30)).isoformat(), open_ended=True), None)
        # Deploy a library that omits "live" while its run is armed → kept + skipped.
        up, deleted, skipped = await runner.apply_sequences([_seq("other", "other")], prune=True)
        assert "live" in skipped and "live" not in deleted
        assert runner.get_sequence("live").name == "live"   # definition survived
    asyncio.run(_run(tmp_path, go))


def test_apply_rejects_unknown_task(tmp_path):
    async def go(runner):
        bad = Sequence(id="x", name="x", steps=[
            SequenceStep(anchor="start", offset_s=0, action=StepAction.START, task_name="ghost"),
            SequenceStep(anchor="stop", offset_s=0, action=StepAction.STOP, task_name="ghost")])
        with pytest.raises(ValueError, match="unknown task"):
            await runner.apply_sequences([bad], prune=True)
    asyncio.run(_run(tmp_path, go))
