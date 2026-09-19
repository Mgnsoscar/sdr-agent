"""RF-fault RECOVERY Phase 3 (docs/rf-fault-recovery.md §7.1/§14d): UNATTENDED auto-restart.

The agent's own tick (SequenceRunner._service_auto_restart) auto-fires restart_run for a faulted run
whose recovery policy is "auto", with NO operator/client present. A budget (config.AUTO_RESTART_BUDGET)
caps attempts and RESETS after the relaunched task transmits healthy for a settle window; on breaker
trip (budget exhausted, or restart_run refuses) the LOUD sequence_rf_fault alarm re-fires and the run
is left RUNNING-faulted for the operator's manual Restart.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import process_manager as pm
from agent import config as agentcfg
from agent.models import (
    ArmSequenceRequest, CreateSequenceRequest, SequenceRun, SequenceState, SequenceStep,
    StepAction, StepFire, TaskConfig,
)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner

REPO_ROOT = str(Path(__file__).resolve().parents[1])

LIVE_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("tx")
     .number("--power", min=-120, max=0, default=-50, live=True)
     .choice("--rf", options=["on", "off"], default="on", live=True, is_rf=True))
args = s.parse()
ctrl = s.live_control(args)
while True:
    for ch in ctrl.drain():
        pass
    time.sleep(0.01)
'''


def _mk(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    # Deterministic knobs for the tests (real defaults are enabled / budget 2 / reset 60 s).
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", True)
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_BUDGET", 2)
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_HEALTHY_RESET_S", 30.0)
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
    return StepFire(anchor="start", offset_s=off, action=action, task_name=task,
                    fire_at=_iso(when), fired_actual=fired,
                    params=params or {}, args=args or [], replace_args=replace)


def _recorder(monkeypatch, runner):
    """Replace runner._fire with an async recorder; returns the (kind, detail) list."""
    fired = []

    async def rec(r, kind, detail=""):
        fired.append((kind, detail))

    monkeypatch.setattr(runner, "_fire", rec)
    return fired


def _faulted_auto_run(runner, now, *, rid="ar", policy="auto", mode="resync", count=0):
    """A RUNNING run whose tx faulted mid-ramp (fired to -70), STOP ahead, under the given policy."""
    T0 = now - timedelta(seconds=10)
    steps = [
        _fire("start", -1.0, T0, fired=_iso(T0), args=["--power", "-90", "--rf", "off"], replace=True),
        _fire("tune", 1.0, T0 + timedelta(seconds=1), fired=_iso(T0 + timedelta(seconds=1)),
              params={"rf": "on"}),
        _fire("tune", 5.0, now - timedelta(seconds=5), fired=_iso(now - timedelta(seconds=5)),
              params={"power": -70}),
        _fire("stop", 0.0, now + timedelta(seconds=10), fired="skipped"),
    ]
    run = SequenceRun(id=rid, sequence_id="s1", sequence_name="sweep", state=SequenceState.RUNNING,
                      on_air_at=_iso(T0), on_air_end=_iso(now + timedelta(seconds=10)), steps=steps,
                      fault="tx: vmcircbuf", fault_task="tx", fault_at=_iso(now - timedelta(seconds=3)),
                      restart_policy=policy, restart_mode=mode, auto_restart_count=count)
    runner._runs[rid] = run
    return run


# ── the auto trigger ──────────────────────────────────────────────────────────

def test_auto_policy_run_is_auto_restarted_by_the_tick(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        # The fault is recovered, the budget counter advanced, a QUIET notice fired (not the alarm).
        assert run.fault == "" and run.auto_restart_count == 1 and run.auto_restart_task == "tx"
        assert any(k == "sequence_auto_restart" for k, _ in fired)
        # A relaunch start fire was inserted at now, born at the reconstructed -70 (RF on).
        relaunch = [s for s in run.steps if s.action == "start" and s.fired_actual is None]
        assert relaunch and float(relaunch[0].args[relaunch[0].args.index("--power") + 1]) == -70.0

    asyncio.run(scenario())


def test_budget_exhaustion_trips_the_breaker_loudly_once(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now, count=2)          # budget (2) already consumed
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        # No further restart; the LOUD alarm re-fires; the run is left RUNNING-faulted for the operator.
        assert run.fault == "tx: vmcircbuf" and run.state == SequenceState.RUNNING
        assert run.auto_restart_count == 2
        assert any(k == "sequence_rf_fault" and "gave up" in d for k, d in fired)
        assert "ar" in runner._auto_gaveup
        # A second tick does NOT re-alarm (fired once).
        fired.clear()
        await runner._service_auto_restart(now)
        assert not fired

    asyncio.run(scenario())


def test_a_restart_refusal_trips_the_breaker_not_a_retry_loop(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        T0 = now - timedelta(seconds=20)
        # STOP already past → a resync restart refuses (no future STOP to recover into).
        steps = [
            _fire("start", 0.0, T0, fired=_iso(T0), args=["--power", "-50", "--rf", "on"], replace=True),
            _fire("stop", 0.0, now - timedelta(seconds=2), fired="skipped"),
        ]
        run = SequenceRun(id="ar", sequence_id="s", sequence_name="late", state=SequenceState.RUNNING,
                          on_air_at=_iso(T0), on_air_end=_iso(now - timedelta(seconds=2)), steps=steps,
                          fault="tx: halt", fault_task="tx", fault_at=_iso(now - timedelta(seconds=8)),
                          restart_policy="auto", restart_mode="resync")
        runner._runs["ar"] = run
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        # The refusal is a breaker trip: loud alarm, no relaunch, counter untouched, marked given-up.
        assert any(k == "sequence_rf_fault" and "gave up" in d for k, d in fired)
        assert run.auto_restart_count == 0
        assert not [s for s in run.steps if s.action == "start" and s.fired_actual is None]
        assert "ar" in runner._auto_gaveup
        # Next tick does not retry the doomed restart.
        fired.clear()
        await runner._service_auto_restart(now)
        assert not fired and run.auto_restart_count == 0

    asyncio.run(scenario())


def test_holding_faulted_run_is_not_auto_restarted(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        run.state = SequenceState.HOLDING                      # a fault during a Hold
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        assert run.fault == "tx: vmcircbuf" and run.auto_restart_count == 0 and not fired

    asyncio.run(scenario())


def test_confirm_and_manual_policy_runs_are_left_for_the_operator(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        for policy in ("confirm", "manual"):
            run = _faulted_auto_run(runner, now, rid="r_" + policy, policy=policy)
            fired = _recorder(monkeypatch, runner)
            await runner._service_auto_restart(now)
            assert run.fault == "tx: vmcircbuf" and run.auto_restart_count == 0 and not fired

    asyncio.run(scenario())


def test_disabled_globally_leaves_a_faulted_auto_run_alone(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", False)
        now = datetime.now(timezone.utc)
        run = _faulted_auto_run(runner, now)
        fired = _recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)
        assert run.fault == "tx: vmcircbuf" and run.auto_restart_count == 0 and not fired

    asyncio.run(scenario())


def test_healthy_settle_resets_the_budget(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)      # reset window 30 s
        now = datetime.now(timezone.utc)
        # A RECOVERED run (no fault) that consumed 1 auto-restart of tx.
        run = SequenceRun(id="ar", sequence_id="s", sequence_name="x", state=SequenceState.RUNNING,
                          on_air_at=_iso(now - timedelta(seconds=60)), steps=[],
                          restart_policy="auto", auto_restart_count=1, auto_restart_task="tx")
        runner._runs["ar"] = run
        monkeypatch.setattr(runner, "_task_healthy", lambda t: True)
        # First tick: stamps the settle marker, no reset yet.
        await runner._service_auto_restart(now)
        assert run.auto_restart_count == 1 and run.auto_restart_healthy_since
        # A tick past the settle window: the budget resets.
        await runner._service_auto_restart(now + timedelta(seconds=31))
        assert run.auto_restart_count == 0 and run.auto_restart_task == ""
        assert not run.auto_restart_healthy_since

    asyncio.run(scenario())


def test_settle_marker_restarts_if_the_task_goes_unhealthy(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = SequenceRun(id="ar", sequence_id="s", sequence_name="x", state=SequenceState.RUNNING,
                          on_air_at=_iso(now - timedelta(seconds=60)), steps=[],
                          restart_policy="auto", auto_restart_count=1, auto_restart_task="tx")
        runner._runs["ar"] = run
        healthy = {"v": True}
        monkeypatch.setattr(runner, "_task_healthy", lambda t: healthy["v"])
        await runner._service_auto_restart(now)
        assert run.auto_restart_healthy_since                   # started settling
        healthy["v"] = False                                   # task dips unhealthy
        await runner._service_auto_restart(now + timedelta(seconds=5))
        assert not run.auto_restart_healthy_since               # settle timer cleared
        assert run.auto_restart_count == 1                      # budget NOT reset

    asyncio.run(scenario())


# ── process-manager suppression (run-owned rf-fault must not raw-relaunch) ──────

class _ExitedProc:
    """A stand-in for the asyncio subprocess: exits immediately with a non-zero code."""
    returncode = 1

    async def wait(self):
        return 1


def test_rf_fault_exit_does_not_crash_restart_even_with_restart_on_crash(tmp_path, monkeypatch):
    """The core suppression: an RF-fault EXIT (Layer-1 done-watcher) must NOT go through the raw
    crash-restart supervisor, even when restart_on_crash is set — recovery is owned by the run's
    policy (restart_run), and a raw relaunch would double-transmit on the single TX channel."""
    async def scenario():
        task = TaskConfig(name="tx", command=["python3", str(tmp_path / "s.py")],
                          working_dir=str(tmp_path), restart_on_crash=True, max_restarts=5)
        mgr = pm.ProcessManager({"tx": task}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        proc._proc = _ExitedProc()
        proc.state = pm.ProcessState.RUNNING                 # an UNINTENDED exit (not a stop)

        async def yes():
            return True

        async def noop(*a, **k):
            return None

        started = []

        async def fake_start(*a, **k):
            started.append(1)

        monkeypatch.setattr(proc, "_is_rf_fault_exit", yes)
        monkeypatch.setattr(proc, "_flag_rf_fault", noop)    # tested elsewhere
        monkeypatch.setattr(proc, "_cleanup", noop)
        monkeypatch.setattr(proc, "start", fake_start)

        await proc._watch()
        assert started == []                                 # SUPPRESSED — no raw relaunch

    asyncio.run(scenario())


def test_ordinary_crash_still_restarts_with_restart_on_crash(tmp_path, monkeypatch):
    """The contrast: a plain (non-rf-fault) crash with restart_on_crash still relaunches — the
    suppression narrows ONLY the rf-fault branch, it does not disable ordinary crash-restart."""
    async def scenario():
        task = TaskConfig(name="tx", command=["python3", str(tmp_path / "s.py")],
                          working_dir=str(tmp_path), restart_on_crash=True, max_restarts=5,
                          restart_delay_s=0.0)
        mgr = pm.ProcessManager({"tx": task}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        proc._proc = _ExitedProc()
        proc.state = pm.ProcessState.RUNNING

        async def no():
            return False

        async def noop(*a, **k):
            return None

        started = []

        async def fake_start(*a, **k):
            started.append(1)

        monkeypatch.setattr(proc, "_is_rf_fault_exit", no)   # NOT an rf-fault → ordinary crash
        monkeypatch.setattr(proc, "_fire_crash_event", noop)
        monkeypatch.setattr(proc, "_cleanup", noop)
        monkeypatch.setattr(proc, "start", fake_start)

        await proc._watch()
        assert started == [1]                                # ordinary crash-restart still fires

    asyncio.run(scenario())


# ── arm plumbing ───────────────────────────────────────────────────────────────

def test_arm_stamps_the_recovery_policy_onto_the_run(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = _mk(tmp_path, monkeypatch)
        seq = await runner.create_sequence(CreateSequenceRequest(name="s", steps=[
            SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx",
                         args=["--power", "-50", "--rf", "on"], replace_args=True),
            SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
        ]))
        now = datetime.now(timezone.utc)
        run = await runner.arm(seq.id, ArmSequenceRequest(
            on_air_at=_iso(now + timedelta(seconds=1)),
            on_air_end=_iso(now + timedelta(seconds=30)),
            restart_policy="auto", restart_mode="replay"),
            _iso(now + timedelta(seconds=30)))
        assert run.restart_policy == "auto" and run.restart_mode == "replay"
        # Default arm (no policy) stays conservative "manual".
        run2 = await runner.arm(seq.id, ArmSequenceRequest(
            on_air_at=_iso(now + timedelta(seconds=40)),
            on_air_end=_iso(now + timedelta(seconds=60))),
            _iso(now + timedelta(seconds=60)))
        assert run2.restart_policy == "manual" and run2.restart_mode == "resync"

    asyncio.run(scenario())
