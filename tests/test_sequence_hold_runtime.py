"""Phase 1 of the Hold step (docs/sequence-hold-step.md §5): the holding RUNTIME.

Drives the real SequenceRunner tick loop against a live mock task to check that a
hold-aware run parks at the hold (holding its last commanded value), that proceed
schedules window B relative to the resume instant, that the max_hold deadman and a
manual abort both drop RF, that a HOLDING run is aborted on restart, and that a
Hold-free sequence still runs straight through (the scheduled no-op).
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import process_manager as pm
from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, ProceedRequest, RampSpec, SequenceRun,
    SequenceState, SequenceStep, StepAction, StepFire, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner

REPO_ROOT = str(Path(__file__).resolve().parents[1])

# A live duration task: START launches it, TUNE retunes its `gain` (device quantises to
# even dB, so `current` is the requested value and `applied` the even one).
LIVE_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("tx")
     .integer("--gain", min=0, max=90, default=30, live=True))
args = s.parse()
ctrl = s.live_control(args)
gain = args.gain
while True:
    for ch in ctrl.drain():
        gain = (int(ch.value) // 2) * 2
        ctrl.report("gain", gain)
    time.sleep(0.01)
'''


def _mk(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    script = tmp_path / "tx.py"
    script.write_text(LIVE_SCRIPT)
    tasks = {"tx": TaskConfig(name="tx", command=["python3", str(script)],
                              working_dir=str(tmp_path), env={"PYTHONPATH": REPO_ROOT})}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json",
                            tmp_path / "runs.json", tmp_path)
    return mgr, runner


def _hold_steps():
    """START @0 → tune gain=41 @1.0 → HOLD @1.2 → (window B: tune 21 @0, tune 15 @0.8) → STOP @0.

    The window-A tune sits 1 s after START so the live task is ready to receive it (the
    generator needs a moment to stand up its live-control loop)."""
    return [
        SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
        SequenceStep(anchor="start", offset_s=1.0, action=StepAction.TUNE,
                     task_name="tx", params={"gain": 41}),
        SequenceStep(anchor="start", offset_s=1.2, action=StepAction.HOLD, task_name=""),
        SequenceStep(anchor="hold", offset_s=0.0, action=StepAction.TUNE,
                     task_name="tx", params={"gain": 21}),
        SequenceStep(anchor="hold", offset_s=0.8, action=StepAction.TUNE,
                     task_name="tx", params={"gain": 15}),
        SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]


# ── Park at the hold, then proceed ───────────────────────────────────────────

def test_hold_aware_run_parks_then_proceeds(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            seq = await runner.create_sequence(CreateSequenceRequest(
                name="loss-of-lock", steps=_hold_steps()))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=0.4)).isoformat(),
                                   open_ended=True, hold_aware=True, max_hold_s=0),
                None,
            )
            rid = run.id

            # Window A fires (START, then tune @+1s), then the run parks at the hold (~+1.6s).
            await asyncio.sleep(2.8)
            held = runner.get_run(rid)
            assert held.state == SequenceState.HOLDING
            assert held.held_actual is not None
            assert held.on_air_end is None and held.open_ended is True
            # Window B is NOT scheduled while holding — no hold-anchored fires yet.
            assert not any(s.anchor == "hold" for s in held.steps)
            assert len(held.window_b_steps) == 3      # 2 hold tunes + the stop
            # The signal HOLDS window A's last commanded value (41), unchanged.
            got = await mgr.get_params("tx")
            assert got["current"]["gain"] == 41

            # Proceed now → window B resolves from the resume instant.
            t_resume = datetime.now(timezone.utc)
            resumed = await runner.proceed(rid, ProceedRequest(proceed_at=t_resume.isoformat()))
            assert resumed.state == SequenceState.RUNNING
            assert resumed.resumed_actual is not None
            assert resumed.on_air_end is not None and resumed.open_ended is False
            assert any(s.anchor == "hold" for s in resumed.steps)     # window B appended

            # The first window-B tune (gain=21 @ resume) applies…
            await asyncio.sleep(0.5)
            got = await mgr.get_params("tx")
            assert got["current"]["gain"] == 21

            # …and the run runs to completion (off-air landed at resume + content).
            await asyncio.sleep(1.4)
            done = runner.get_run(rid)
            assert done.state == SequenceState.COMPLETED
            assert not mgr.is_running("tx")
        finally:
            await runner.shutdown()
            if mgr.is_running("tx"):
                await mgr.stop("tx")
            await mgr.shutdown()

    asyncio.run(scenario())


# ── Proceed resolves a hold-anchored down-ramp (the motivating case) ─────────

def test_proceed_resolves_a_window_b_ramp(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            steps = [
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
                SequenceStep(anchor="start", offset_s=1.0, action=StepAction.TUNE,
                             task_name="tx", params={"gain": 60}),
                SequenceStep(anchor="start", offset_s=1.2, action=StepAction.HOLD, task_name=""),
                # Window B: a down-ramp 40 → 20 (3 pts @ 0.0/0.5/1.0), then off-air.
                SequenceStep(anchor="hold", offset_s=0.0, action=StepAction.RAMP, task_name="tx",
                             ramp=RampSpec(param="gain", start=40, stop=20, steps=2,
                                           hold_s=0.5, mode="tune")),
                SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
            ]
            seq = await runner.create_sequence(CreateSequenceRequest(name="down-ramp", steps=steps))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=0.4)).isoformat(),
                                   open_ended=True, hold_aware=True, max_hold_s=0),
                None,
            )
            rid = run.id
            await asyncio.sleep(2.8)
            assert runner.get_run(rid).state == SequenceState.HOLDING
            assert (await mgr.get_params("tx"))["current"]["gain"] == 60   # held

            t_resume = datetime.now(timezone.utc)
            resumed = await runner.proceed(rid, ProceedRequest(proceed_at=t_resume.isoformat()))
            # The ramp expanded to hold-anchored tune fires from the resume instant.
            ramp_fires = [s for s in resumed.steps if s.anchor == "hold" and s.action == "tune"]
            assert len(ramp_fires) == 3

            await asyncio.sleep(0.3)
            assert (await mgr.get_params("tx"))["current"]["gain"] == 40   # ramp top at resume
            await asyncio.sleep(1.4)
            done = runner.get_run(rid)
            assert done.state == SequenceState.COMPLETED
            assert not mgr.is_running("tx")
        finally:
            await runner.shutdown()
            if mgr.is_running("tx"):
                await mgr.stop("tx")
            await mgr.shutdown()

    asyncio.run(scenario())


# ── The max-hold deadman ─────────────────────────────────────────────────────

def test_max_hold_deadman_auto_aborts(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            seq = await runner.create_sequence(CreateSequenceRequest(
                name="deadman", steps=_hold_steps()))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=0.4)).isoformat(),
                                   open_ended=True, hold_aware=True, max_hold_s=1.0),
                None,
            )
            rid = run.id
            # Reaches HOLDING ~+1.6s; deadman (1s) trips ~+2.6s → auto-abort.
            await asyncio.sleep(3.7)
            done = runner.get_run(rid)
            assert done.state == SequenceState.ABORTED
            assert not mgr.is_running("tx")
        finally:
            await runner.shutdown()
            if mgr.is_running("tx"):
                await mgr.stop("tx")
            await mgr.shutdown()

    asyncio.run(scenario())


# ── Abort while holding ──────────────────────────────────────────────────────

def test_abort_while_holding_drops_rf(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            seq = await runner.create_sequence(CreateSequenceRequest(
                name="abortable", steps=_hold_steps()))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=0.4)).isoformat(),
                                   open_ended=True, hold_aware=True, max_hold_s=0),
                None,
            )
            rid = run.id
            await asyncio.sleep(2.2)
            assert runner.get_run(rid).state == SequenceState.HOLDING
            assert mgr.is_running("tx")

            aborted = await runner.cancel_or_abort(rid)
            assert aborted.state == SequenceState.ABORTED
            assert not mgr.is_running("tx")
        finally:
            await runner.shutdown()
            if mgr.is_running("tx"):
                await mgr.stop("tx")
            await mgr.shutdown()

    asyncio.run(scenario())


def test_proceed_requires_holding(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            seq = await runner.create_sequence(CreateSequenceRequest(
                name="not-holding", steps=_hold_steps()))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=0.4)).isoformat(),
                                   open_ended=True, hold_aware=True, max_hold_s=0),
                None,
            )
            # Still ARMED/RUNNING (not yet at the hold) → proceed is refused.
            try:
                await runner.proceed(run.id, ProceedRequest(
                    proceed_at=datetime.now(timezone.utc).isoformat()))
                assert False, "proceed should refuse a run that is not holding"
            except ValueError as exc:
                assert "not holding" in str(exc)
        finally:
            await runner.shutdown()
            if mgr.is_running("tx"):
                await mgr.stop("tx")
            await mgr.shutdown()

    asyncio.run(scenario())


# ── Restart aborts a HOLDING run (fail-safe) ─────────────────────────────────

def test_restart_aborts_a_holding_run(tmp_path, monkeypatch):
    async def scenario():
        # Persist a run already in HOLDING (as if the agent went down mid-hold).
        runs_path = tmp_path / "runs.json"
        now = datetime.now(timezone.utc)
        held = SequenceRun(
            id="run_held1", sequence_id="seq1", sequence_name="held",
            state=SequenceState.HOLDING,
            on_air_at=(now - timedelta(seconds=30)).isoformat(),
            open_ended=True, hold_aware=True, hold_at_offset_s=0.5,
            held_actual=(now - timedelta(seconds=20)).isoformat(),
            steps=[StepFire(anchor="start", offset_s=0.0, action="start", task_name="tx",
                            fire_at=(now - timedelta(seconds=30)).isoformat(),
                            fired_actual=(now - timedelta(seconds=30)).isoformat())],
            window_b_steps=[SequenceStep(anchor="stop", offset_s=0.0,
                                         action=StepAction.STOP, task_name="tx")],
        )
        runs_path.write_text(json.dumps({"runs": [held.model_dump()]}))

        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()          # loads runs + reconciles
        try:
            reloaded = runner.get_run("run_held1")
            assert reloaded.state == SequenceState.ABORTED
        finally:
            await runner.shutdown()
            await mgr.shutdown()

    asyncio.run(scenario())


# ── The scheduled no-op: a Hold-free sequence runs straight through ───────────

def test_hold_free_sequence_runs_straight_through(tmp_path, monkeypatch):
    # The scheduled path compiles the Hold out (client-side) into an ordinary two-anchor
    # list; the agent runs it normally and never enters HOLDING.
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            steps = [
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
                SequenceStep(anchor="start", offset_s=0.2, action=StepAction.TUNE,
                             task_name="tx", params={"gain": 33}),
                SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
            ]
            seq = await runner.create_sequence(CreateSequenceRequest(name="plain", steps=steps))
            now = datetime.now(timezone.utc)
            run = await runner.arm(
                seq.id,
                ArmSequenceRequest(on_air_at=(now + timedelta(seconds=0.4)).isoformat(),
                                   on_air_end=(now + timedelta(seconds=1.4)).isoformat()),
                (now + timedelta(seconds=1.4)).isoformat(),
            )
            rid = run.id
            await asyncio.sleep(2.0)
            done = runner.get_run(rid)
            assert done.state == SequenceState.COMPLETED       # never HOLDING
            assert done.held_actual is None and done.hold_aware is False
            assert not mgr.is_running("tx")
        finally:
            await runner.shutdown()
            if mgr.is_running("tx"):
                await mgr.stop("tx")
            await mgr.shutdown()

    asyncio.run(scenario())
