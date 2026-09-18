"""RF-fault RECOVERY Phase 2 (docs/rf-fault-recovery.md §7): SequenceRunner.restart_run.

Deterministic unit tests over a constructed faulted run (the crash-time state Phase-1 on_task_fault
leaves: run.fault stamped, the faulted task's un-fired steps 'skipped'), plus a LIVE end-to-end that
faults a real mock task mid-ramp and restarts it back onto the schedule.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import process_manager as pm
from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, RestartRequest, SequenceRun, SequenceState,
    SequenceStep, StepAction, StepFire, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner

REPO_ROOT = str(Path(__file__).resolve().parents[1])

# A live duration task with a calibrated-style --power and an RF gate, live-tunable.
LIVE_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("tx")
     .number("--power", min=-120, max=0, default=-50, live=True)
     .choice("--rf", options=["on", "off"], default="on", live=True, is_rf=True))
args = s.parse()
ctrl = s.live_control(args)
power, rf = args.power, args.rf
while True:
    for ch in ctrl.drain():
        if ch.name == "power":
            power = float(ch.value); ctrl.report("power", power)
        elif ch.name == "rf":
            rf = ch.value; ctrl.report("rf", rf)
    time.sleep(0.01)
'''


def _mk(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    script = tmp_path / "tx.py"
    script.write_text(LIVE_SCRIPT)
    tasks = {"tx": TaskConfig(name="tx", command=["python3", str(script), "--power", "-90",
                                                  "--rf", "off"],
                              working_dir=str(tmp_path), env={"PYTHONPATH": REPO_ROOT})}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json",
                            tmp_path / "runs.json", tmp_path)
    return mgr, runner


def _iso(dt):
    return dt.isoformat()


def _fire(action, off, when, *, fired, task="tx", params=None, args=None, replace=False):
    """Build one StepFire. `fired`: an ISO string (fired), 'skipped', or None (pending)."""
    return StepFire(anchor="start", offset_s=off, action=action, task_name=task,
                    fire_at=_iso(when), fired_actual=fired,
                    params=params or {}, args=args or [], replace_args=replace)


def _faulted_run(runner, now, *, rid="r1"):
    """Install a RUNNING run whose `tx` ramp fired -90→-70, then faulted at now-3s with a still
    -50 point and the STOP ahead (both 'skipped' by on_task_fault). Returns the run."""
    T0 = now - timedelta(seconds=10)
    steps = [
        _fire("start", -1.0, T0, fired=_iso(T0),
              args=["--power", "-90", "--rf", "off"], replace=True),
        _fire("tune", 0.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
              params={"rf": "on"}),
        _fire("tune", 0.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
              params={"power": -90}),
        _fire("tune", 5.0, now - timedelta(seconds=5), fired=_iso(now - timedelta(seconds=5)),
              params={"power": -70}),                       # last FIRED level → L_now
        _fire("tune", 12.0, now + timedelta(seconds=2), fired="skipped",
              params={"power": -50}),                       # future ramp point (fault-skipped)
        _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),   # STOP (fault-skipped)
    ]
    run = SequenceRun(id=rid, sequence_id="s1", sequence_name="chirp sweep",
                      state=SequenceState.RUNNING,
                      on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)),
                      steps=steps, fault="tx: vmcircbuf", fault_task="tx",
                      fault_at=_iso(now - timedelta(seconds=3)))
    runner._runs[rid] = run
    return run


# ── L_now reconstruction + resync re-instatement + the relaunch fire ─────────────

def test_restart_resync_reconstructs_level_reinstates_future_and_relaunches(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_run(runner, now)
        out = await runner.restart_run("r1", RestartRequest(mode="resync", restart_at=_iso(now)))

        # The fault is cleared and the run is still RUNNING.
        assert out.fault == "" and out.fault_task == "" and out.state == SequenceState.RUNNING
        # The future ramp point + the STOP were re-instated (no longer 'skipped'), fire_at unchanged.
        future = [s for s in out.steps if s.action == "tune" and s.params.get("power") == -50]
        assert future and future[0].fired_actual is None
        stop = [s for s in out.steps if s.action == "stop"][0]
        assert stop.fired_actual is None and stop.fire_at == _iso(now + timedelta(seconds=10))
        # Off-air is UNCHANGED (resync).
        assert out.on_air_end == _iso(now + timedelta(seconds=10))
        # A synthetic relaunch START at `now`, born at L_now=-70 with RF on, replace_args.
        relaunch = [s for s in out.steps
                    if s.action == "start" and s.fired_actual is None][0]
        assert relaunch.fire_at == _iso(now) and relaunch.replace_args
        assert "--power" in relaunch.args
        pi = relaunch.args.index("--power")
        assert float(relaunch.args[pi + 1]) == -70.0
        ri = relaunch.args.index("--rf")
        assert relaunch.args[ri + 1] == "on"                # RF forced ON (was 'off' at launch)

    asyncio.run(scenario())


def test_restart_replay_shifts_future_fires_and_off_air(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        _faulted_run(runner, now)                            # fault_at = now-3s → downtime 3s
        out = await runner.restart_run("r1", RestartRequest(mode="replay", restart_at=_iso(now)))
        # The future ramp point + STOP shifted LATER by the 3 s downtime.
        future = [s for s in out.steps if s.action == "tune" and s.params.get("power") == -50][0]
        assert future.fire_at == _iso(now + timedelta(seconds=2 + 3))
        stop = [s for s in out.steps if s.action == "stop"][0]
        assert stop.fire_at == _iso(now + timedelta(seconds=10 + 3))
        # Off-air floated +3 s.
        assert out.on_air_end == _iso(now + timedelta(seconds=10 + 3))

    asyncio.run(scenario())


def test_restart_reconstructs_launch_level_when_no_tune_fired(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=5)
        # A fixed-power task: only a launch (--power -45), no ramp; faulted.
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-45", "--rf", "on"],
                  replace=True),
            _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),
        ]
        run = SequenceRun(id="r2", sequence_id="s", sequence_name="cw", state=SequenceState.RUNNING,
                          on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)),
                          steps=steps, fault="tx: halt", fault_task="tx", fault_at=_iso(now))
        runner._runs["r2"] = run
        out = await runner.restart_run("r2", RestartRequest(restart_at=_iso(now)))
        relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
        pi = relaunch.args.index("--power")
        assert float(relaunch.args[pi + 1]) == -45.0         # the launch level

    asyncio.run(scenario())


def test_restart_replay_refuses_a_channel_collision(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        _faulted_run(runner, now)                            # off-air now+10, replay → now+13
        # A peer run occupying the channel at now+11..now+20 → the +3 s replay end (now+13) collides.
        peer = SequenceRun(id="peer", sequence_id="s2", sequence_name="next",
                           state=SequenceState.ARMED,
                           on_air_at=_iso(now + timedelta(seconds=11)),
                           on_air_end=_iso(now + timedelta(seconds=20)),
                           steps=[_fire("start", 0.0, now + timedelta(seconds=11), fired=None)])
        runner._runs["peer"] = peer
        try:
            await runner.restart_run("r1", RestartRequest(mode="replay", restart_at=_iso(now)))
            assert False, "expected a collision refusal"
        except ValueError as exc:
            assert "overlapping" in str(exc) and "resync" in str(exc)
        # resync (off-air unchanged) is still allowed alongside the peer.
        out = await runner.restart_run("r1", RestartRequest(mode="resync", restart_at=_iso(now)))
        assert out.fault == ""

    asyncio.run(scenario())


def test_restart_guards(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        # Unknown run → KeyError (→404).
        try:
            await runner.restart_run("nope", RestartRequest())
            assert False
        except KeyError:
            pass
        # A run with no fault → ValueError (→409).
        healthy = SequenceRun(id="ok", sequence_id="s", sequence_name="x",
                              state=SequenceState.RUNNING, on_air_at=_iso(now), steps=[])
        runner._runs["ok"] = healthy
        try:
            await runner.restart_run("ok", RestartRequest())
            assert False
        except ValueError as exc:
            assert "no RF fault" in str(exc)
        # A faulted run in a non-RUNNING state → ValueError.
        done = SequenceRun(id="done", sequence_id="s", sequence_name="x",
                           state=SequenceState.COMPLETED, on_air_at=_iso(now),
                           steps=[], fault="tx: x", fault_task="tx", fault_at=_iso(now))
        runner._runs["done"] = done
        try:
            await runner.restart_run("done", RestartRequest())
            assert False
        except ValueError as exc:
            assert "not running" in str(exc)
        # A bad mode → ValueError.
        _faulted_run(runner, now, rid="r3")
        try:
            await runner.restart_run("r3", RestartRequest(mode="sideways"))
            assert False
        except ValueError as exc:
            assert "unknown restart mode" in str(exc)

    asyncio.run(scenario())


# ── Live end-to-end: fault a real ramping task mid-run, restart it back on schedule ──

def test_restart_live_recovers_a_faulted_ramp(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            steps = [
                SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx",
                             args=["--power", "-90", "--rf", "off"], replace_args=True),
                SequenceStep(anchor="start", offset_s=1.0, action=StepAction.TUNE, task_name="tx",
                             params={"rf": "on"}),
                SequenceStep(anchor="start", offset_s=1.0, action=StepAction.TUNE, task_name="tx",
                             params={"power": -90}),
                SequenceStep(anchor="start", offset_s=2.0, action=StepAction.TUNE, task_name="tx",
                             params={"power": -70}),
                SequenceStep(anchor="start", offset_s=6.0, action=StepAction.TUNE, task_name="tx",
                             params={"power": -50}),
                SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
            ]
            seq = await runner.create_sequence(CreateSequenceRequest(name="sweep", steps=steps))
            now = datetime.now(timezone.utc)
            run = await runner.arm(seq.id, ArmSequenceRequest(
                on_air_at=_iso(now + timedelta(seconds=0.5)),
                on_air_end=_iso(now + timedelta(seconds=9)),
                open_ended=False), _iso(now + timedelta(seconds=9)))
            rid = run.id

            # Let it launch, un-mute, and ramp to -70 (the -50 point is at ~+6.5s, still ahead).
            await asyncio.sleep(3.5)
            assert mgr.is_running("tx")
            # Simulate the Phase-1 fault coupling at -70: stamp the run + kill the task.
            await runner.on_task_fault("tx", "vmcircbuf")
            await mgr.stop("tx", source="sequence")
            await asyncio.sleep(0.5)
            faulted = runner.get_run(rid)
            assert faulted.fault and not mgr.is_running("tx")

            # Restart (resync): relaunch at the -70 it faulted from, RF on, still on the -50/STOP.
            await runner.restart_run(rid, RestartRequest(mode="resync"))
            await asyncio.sleep(2.5)
            assert mgr.is_running("tx")                       # the task is back
            recovered = runner.get_run(rid)
            assert recovered.fault == ""                       # cleared

            # The -50 point + STOP still fire on schedule → the run completes.
            await asyncio.sleep(5.0)
            done = runner.get_run(rid)
            assert done.state == SequenceState.COMPLETED
            assert not mgr.is_running("tx")
        finally:
            await runner.shutdown()
            await mgr.shutdown()

    asyncio.run(scenario())
