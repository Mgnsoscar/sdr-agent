"""Regression tests for the RF-fault arc adversarial review (docs/rf-fault-recovery.md §14f).

One test per confirmed finding (numbered as in the review record), each asserting the FIXED
behaviour. The HIGH ones are the verifiers' own reproductions inverted: they drive the real
SequenceRunner + ProcessManager over real subprocesses (a paramkit live script), so a regression
shows up as a real process left on air, a wrong --power on a real relaunch command, or a tune
that never reaches a real control socket — not as a stub disagreement.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_sequence_auto_restart as T          # LIVE_SCRIPT / _mk / _fire / _iso / _faulted_auto_run
from agent import config as agentcfg
from agent import process_manager as pm
from agent import recovery
from agent import system as sysmon
from agent.log_manager import LogManager
from agent.models import (ArmSequenceRequest, CreateSequenceRequest, PlanItem, ProceedRequest,
                          ProcessState, RestartRequest, Sequence, SequenceRun, SequenceState,
                          SequenceStep, StartRequest, StepAction, TaskConfig, TaskHealth)
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner, _RestartDeferred
from paramkit.txhealth import TRANSMITTING_MARKER

PEER = "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(0.1)\n"


class _FakeProc:
    def __init__(self, code=1, alive=False):
        self.pid = os.getpid()
        self.returncode = None if alive else code
        self._code = code

    async def wait(self):
        self.returncode = self._code
        return self._code


def _stub_start(proc):
    calls = []

    async def start(request=None):
        calls.append(request)
        proc.state = ProcessState.RUNNING
        proc._proc = _FakeProc(alive=True)
        proc._operator_stop_requested = False
        proc._stop_requested = False
    proc.start = start
    return calls


def _two_task(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", True)
    (tmp_path / "tx.py").write_text(T.LIVE_SCRIPT)
    (tmp_path / "zpeer.py").write_text(PEER)
    tasks = {
        "tx": TaskConfig(name="tx", command=["python3", str(tmp_path / "tx.py"), "--power", "-90", "--rf", "off"],
                         working_dir=str(tmp_path), env={"PYTHONPATH": T.REPO_ROOT}),
        "zpeer": TaskConfig(name="zpeer", command=["python3", str(tmp_path / "zpeer.py")],
                            working_dir=str(tmp_path)),
    }
    mgr = ProcessManager(tasks, tmp_path, "u")
    runner = SequenceRunner(mgr, "u", tmp_path / "seq.json", tmp_path / "runs.json", tmp_path)
    return mgr, runner


# ── #6 (HIGH, found by four dimensions): abort racing a restart relaunch ─────────────────────────

def test_abort_racing_a_restart_relaunch_leaves_nothing_on_air(tmp_path, monkeypatch):
    """The abort's stop loop awaits a SIGTERM-ignoring peer; across that await the tick fires the
    committed relaunch. Before the fix tx came up RF-on in an ABORTED run with its STOP never firing."""
    monkeypatch.setattr(pm.ManagedProcess.stop, "__defaults__", (1.0,))   # 1 s SIGKILL grace

    async def scenario():
        mgr, runner = _two_task(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = T._faulted_auto_run(runner, now, policy="manual")
        T0 = now - timedelta(seconds=10)
        run.steps.insert(0, T._fire("start", -1.0, T0, fired=T._iso(T0), task="zpeer"))
        run.steps.append(T._fire("stop", 0.0, now + timedelta(seconds=10), fired=None, task="zpeer"))
        await mgr.start("zpeer")
        await asyncio.sleep(0.5)
        await runner.restart_run(run.id, RestartRequest(mode="resync"))
        assert [s for s in run.steps if s.task_name == "tx" and s.action == "start" and s.fired_actual is None]
        abort = asyncio.create_task(runner.cancel_or_abort(run.id))
        await asyncio.sleep(0.3)
        assert not abort.done()                          # inside `await manager.stop('zpeer')`
        await runner._tick()                             # the loop keeps ticking meanwhile
        await asyncio.sleep(0.3)
        await abort
        for _ in range(3):
            await runner._tick()
            await asyncio.sleep(0.2)
        try:
            assert run.state == SequenceState.ABORTED
            assert not mgr.is_running("tx") and not mgr.is_process_alive("tx")
            assert not mgr.is_running("zpeer")
        finally:
            await mgr.shutdown()
    asyncio.run(scenario())


def test_abort_landing_inside_the_relaunch_launch_window_still_stops_it(tmp_path, monkeypatch):
    """The tick is inside the relaunch's attenuator pre-command when the operator aborts: the launch
    completes AFTER the abort — and is stopped right away instead of transmitting ownerless."""
    async def scenario():
        mgr, runner = _two_task(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = T._faulted_auto_run(runner, now, policy="manual")
        await runner.restart_run(run.id, RestartRequest(mode="resync"))
        gate, entered = asyncio.Event(), asyncio.Event()
        orig = mgr._gate_precommand

        async def slow_gate(name, **kw):
            entered.set()
            await gate.wait()
            return await orig(name, **kw)
        monkeypatch.setattr(mgr, "_gate_precommand", slow_gate)
        tick = asyncio.create_task(runner._tick())
        await entered.wait()
        await runner.cancel_or_abort(run.id)
        assert run.state == SequenceState.ABORTED
        gate.set()
        await tick
        await asyncio.sleep(0.5)
        try:
            assert not mgr.is_running("tx") and not mgr.is_process_alive("tx")
        finally:
            await mgr.shutdown()
    asyncio.run(scenario())


# ── #5 (HIGH): hold_now's skips are not the schedule's level ─────────────────────────────────────

def _hold_steps():
    S = SequenceStep
    return [
        S(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx",
          args=["--power", "-90", "--rf", "off"], replace_args=True),
        S(anchor="start", offset_s=1.0, action=StepAction.TUNE, task_name="tx", params={"rf": "on"}),
        S(anchor="start", offset_s=1.0, action=StepAction.TUNE, task_name="tx", params={"power": -90}),
        S(anchor="start", offset_s=1.5, action=StepAction.TUNE, task_name="tx", params={"power": -80}),
        S(anchor="start", offset_s=2.0, action=StepAction.TUNE, task_name="tx", params={"power": -70}),
        S(anchor="start", offset_s=4.0, action=StepAction.TUNE, task_name="tx", params={"power": -60}),
        S(anchor="start", offset_s=5.0, action=StepAction.TUNE, task_name="tx", params={"power": -50}),
        S(anchor="start", offset_s=6.0, action=StepAction.TUNE, task_name="tx", params={"power": -40}),
        S(anchor="start", offset_s=7.0, action=StepAction.TUNE, task_name="tx", params={"power": -30}),
        S(anchor="start", offset_s=8.0, action=StepAction.HOLD, task_name=""),
        S(anchor="hold", offset_s=6.0, action=StepAction.TUNE, task_name="tx", params={"rf": "on"}),
        S(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
    ]


def test_resync_after_hold_now_relaunches_at_the_held_level_not_the_ramp_top(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        await mgr.startup()
        await runner.startup()
        try:
            seq = await runner.create_sequence(CreateSequenceRequest(name="hn", steps=_hold_steps()))
            now = datetime.now(timezone.utc)
            run = await runner.arm(seq.id, ArmSequenceRequest(
                on_air_at=T._iso(now + timedelta(seconds=0.5)), open_ended=True,
                hold_aware=True, max_hold_s=0), None)
            rid = run.id
            await asyncio.sleep(3.2)                     # START, RF on, -90, -80, -70 fired
            assert (await mgr.get_params("tx"))["current"]["power"] == -70
            held = await runner.hold_now(rid)            # freeze at -70; -60..-30 skipped
            assert held.state == SequenceState.HOLDING
            assert sorted(s.params["power"] for s in held.steps
                          if s.fired_actual == "skipped:hold") == [-60, -50, -40, -30]
            await asyncio.sleep(5.5)                     # their original fire_at pass
            resumed = await runner.proceed(rid, ProceedRequest(proceed_at=T._iso(datetime.now(timezone.utc))))
            assert resumed.state == SequenceState.RUNNING
            await asyncio.sleep(0.5)
            await runner.on_task_fault("tx", "vmcircbuf")
            await mgr.stop("tx", source="sequence")
            await asyncio.sleep(0.5)
            out = await runner.restart_run(rid, RestartRequest(mode="resync"))
            relaunch = [s for s in out.steps if s.action == "start" and s.fired_actual is None][0]
            assert relaunch.args[relaunch.args.index("--power") + 1] == "-70", relaunch.args
            # the hold-skipped up-ramp points are NOT re-instated either
            assert not [s for s in out.steps if s.params.get("power") in (-60, -50, -40, -30)
                        and s.fired_actual is None]
            await asyncio.sleep(2.0)
            assert (await mgr.get_params("tx"))["current"]["power"] == -70
        finally:
            await runner.shutdown()
            await mgr.shutdown()
    asyncio.run(scenario())


# ── #4 (HIGH): a re-instated tune inside the relaunched script's bind window ─────────────────────

SLOW_SCRIPT = '''\
import time
from paramkit import Script
s = (Script("tx")
     .number("--power", min=-120, max=0, default=-50, live=True)
     .choice("--rf", options=["on", "off"], default="on", live=True, is_rf=True))
args = s.parse()
time.sleep(2.0)                      # the IQ build: every real generator binds AFTER it
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


def test_reinstated_tune_inside_the_bind_window_is_deferred_not_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    (tmp_path / "tx.py").write_text(SLOW_SCRIPT)
    tasks = {"tx": TaskConfig(name="tx", command=["python3", str(tmp_path / "tx.py"), "--power", "-90", "--rf", "off"],
                              working_dir=str(tmp_path), env={"PYTHONPATH": T.REPO_ROOT})}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    runner = SequenceRunner(mgr, "unit-a", tmp_path / "seq.json", tmp_path / "runs.json", tmp_path)

    async def scenario():
        await mgr.startup()
        await runner.startup()
        try:
            steps = [
                SequenceStep(anchor="start", offset_s=-3.0, action=StepAction.START, task_name="tx",
                             args=["--power", "-90", "--rf", "on"], replace_args=True),
                SequenceStep(anchor="start", offset_s=1.0, action=StepAction.TUNE, task_name="tx", params={"power": -70}),
                SequenceStep(anchor="start", offset_s=6.0, action=StepAction.TUNE, task_name="tx", params={"power": -50}),
                SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx"),
            ]
            seq = await runner.create_sequence(CreateSequenceRequest(name="sweep", steps=steps))
            now = datetime.now(timezone.utc)
            T0 = now + timedelta(seconds=3.5)
            end = T0 + timedelta(seconds=14)
            run = await runner.arm(seq.id, ArmSequenceRequest(
                on_air_at=T._iso(T0), on_air_end=T._iso(end), open_ended=False), T._iso(end))
            rid = run.id
            await asyncio.sleep(6.0)                                  # ~T0+2.5: bound, -70 applied
            assert (await mgr.get_params("tx"))["current"]["power"] == -70
            await runner.on_task_fault("tx", "vmcircbuf")
            await mgr.stop("tx", source="sequence")
            await asyncio.sleep(2.5)                                  # ~T0+5: -50 is 1 s ahead (< 2 s bind)
            rec = await runner.restart_run(rid, RestartRequest(mode="resync"))
            assert rec.fault == ""
            await asyncio.sleep(4.5)                                  # relaunch bound ~T0+7.5, tune deferred to it
            r = runner.get_run(rid)
            fifty = [s for s in r.steps if s.action == "tune" and s.params.get("power") == -50][0]
            assert fifty.fired_actual not in (None, "skipped")
            assert (await mgr.get_params("tx"))["current"]["power"] == -50
        finally:
            await runner.shutdown()
            await mgr.shutdown()
    asyncio.run(scenario())


# ── #2 (HIGH): a standalone relaunch reproduces the LIVE state ───────────────────────────────────

def test_standalone_relaunch_carries_the_live_tuned_state(tmp_path):
    async def scenario():
        (tmp_path / "tx.py").write_text(T.LIVE_SCRIPT)
        cfgt = TaskConfig(name="tx", command=["python3", str(tmp_path / "tx.py")], working_dir=str(tmp_path),
                          auto_restart_on_fault=True, restart_delay_s=0.0)
        mgr = pm.ProcessManager({"tx": cfgt}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        proc._launch_hook = mgr.relaunch
        launches = []
        async def fake_start(request=None):
            req = request or StartRequest()
            launches.append(pm._build_command(cfgt.command, req.args, req.replace_args))
            proc._last_request = request
            proc._auto_restart_override = req.auto_restart_on_fault
            proc._live_applied = {}
            proc.state = ProcessState.RUNNING
            proc._proc = _FakeProc(alive=True)
            proc._stop_requested = proc._operator_stop_requested = False
        proc.start = fake_start
        async def fake_set_params(values, wait=1.0):
            return {"ok": True, "applied": values, "rejected": {}}
        proc.set_params = fake_set_params
        assert mgr._rf_gate("tx") is not None
        await mgr.start("tx", StartRequest(args=["--power", "-50", "--rf", "on"], replace_args=True))
        await mgr.set_params("tx", {"power": -100.0, "rf": "off"})    # operator mutes + lowers it
        proc._proc = _FakeProc(code=0)
        proc.log.current.write_bytes(b"stuck\nvmcircbuf: no space\n")
        await mgr._scan_task_health(proc)
        await asyncio.sleep(0.05)
        assert proc.health == TaskHealth.RF_FAULT.value
        assert len(launches) == 2, launches
        cmd = launches[1]
        assert cmd[cmd.index("--rf") + 1] == "off"                    # back MUTED, as it was
        assert cmd[cmd.index("--power") + 1] == "-100"                # at the LOWERED level
        assert mgr._gate_state["tx"]["rf_on"] is False                # attenuator positioned for that
    asyncio.run(scenario())


def test_standalone_relaunch_stands_down_when_tuned_but_schema_unreadable(tmp_path):
    async def scenario():
        (tmp_path / "tx.py").write_text(T.LIVE_SCRIPT)
        cfgt = TaskConfig(name="tx", command=["python3", str(tmp_path / "tx.py")], working_dir=str(tmp_path),
                          auto_restart_on_fault=True, restart_delay_s=0.0)
        mgr = pm.ProcessManager({"tx": cfgt}, tmp_path, unit_id="u")
        proc = mgr._procs["tx"]
        calls = _stub_start(proc)
        proc._live_applied = {"rf": "off"}
        mgr._script_spec = lambda n: None
        await mgr.relaunch("tx", StartRequest(args=["--power", "-50", "--rf", "on"], replace_args=True))
        assert calls == []                                            # never relaunched hot
    asyncio.run(scenario())


# ── #3 (HIGH): PUT /library keeps the authored recovery policy ───────────────────────────────────

def test_apply_sequences_keeps_the_recovery_policy(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        seq = Sequence(id="s-auto", name="overnight", steps=[
            SequenceStep(anchor="start", offset_s=0.0, action=StepAction.START, task_name="tx"),
            SequenceStep(anchor="stop", offset_s=0.0, action=StepAction.STOP, task_name="tx")],
            recovery_policy="auto", recovery_mode="replay")
        await runner.apply_sequences([seq], prune=False)
        got = runner.get_sequence("s-auto")
        assert (got.recovery_policy, got.recovery_mode) == ("auto", "replay")
    asyncio.run(scenario())


# ── #33 / #12 / #11: PANIC, shutdown and an operator stop beat a pending relaunch ────────────────

def _relaunchable(tmp_path, delay=3.0):
    (tmp_path / "tx.py").write_text(T.LIVE_SCRIPT)
    cfgt = TaskConfig(name="tx", command=["python3", str(tmp_path / "tx.py")], working_dir=str(tmp_path),
                      auto_restart_on_fault=True, restart_delay_s=delay)
    mgr = pm.ProcessManager({"tx": cfgt}, tmp_path, unit_id="u")
    return mgr, mgr._procs["tx"]


class _NoRuns:
    async def abort_all_active(self, reason):
        return []

    async def cancel_all_active(self, reason):
        return []


def test_panic_cancels_a_pending_exit_path_relaunch(tmp_path, monkeypatch):
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", True)

    async def scenario():
        mgr, proc = _relaunchable(tmp_path, delay=1.0)
        calls = _stub_start(proc)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        proc._watcher_task = asyncio.create_task(proc._maybe_auto_restart_standalone())
        await asyncio.sleep(0.1)                         # sleeping its settle delay ('crashed')
        assert proc._in_restart_delay
        await recovery.panic_stop(mgr, _NoRuns(), _NoRuns(), "u")
        await asyncio.sleep(1.5)
        assert calls == []                               # nothing came back on air after PANIC
        assert proc.state == ProcessState.STOPPED
    asyncio.run(scenario())


def test_panic_cancels_a_pending_wedge_path_relaunch(tmp_path, monkeypatch):
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", True)

    async def scenario():
        mgr, proc = _relaunchable(tmp_path, delay=1.0)
        calls = _stub_start(proc)
        proc.state = ProcessState.STOPPED                # after the auto-drop
        proc._proc = _FakeProc(code=0)
        proc._relaunch_task = asyncio.create_task(proc._maybe_auto_restart_standalone())
        await asyncio.sleep(0.1)
        await recovery.panic_stop(mgr, _NoRuns(), _NoRuns(), "u")
        await asyncio.sleep(1.5)
        assert calls == []
    asyncio.run(scenario())


def test_shutdown_cancels_an_exit_path_relaunch(tmp_path, monkeypatch):
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", True)

    async def scenario():
        mgr, proc = _relaunchable(tmp_path, delay=1.0)
        calls = _stub_start(proc)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        proc._watcher_task = asyncio.create_task(proc._maybe_auto_restart_standalone())
        await asyncio.sleep(0.1)
        await mgr.shutdown()
        await asyncio.sleep(1.5)
        assert calls == []
    asyncio.run(scenario())


def test_relaunch_woken_after_shutdown_began_stands_down(tmp_path, monkeypatch):
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", True)

    async def scenario():
        mgr, proc = _relaunchable(tmp_path, delay=0.3)
        calls = _stub_start(proc)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        t = asyncio.create_task(proc._maybe_auto_restart_standalone())
        mgr._shutdown_flag.set()                         # the flag alone, no cancel
        await t
        assert calls == []
    asyncio.run(scenario())


def test_operator_stop_during_the_relaunch_precommand_abandons_it(tmp_path, monkeypatch):
    async def scenario():
        mgr, proc = _relaunchable(tmp_path, delay=0.0)
        calls = _stub_start(proc)
        proc.state = ProcessState.STOPPED
        gate, entered = asyncio.Event(), asyncio.Event()

        async def slow_gate(name, **kw):
            entered.set()
            await gate.wait()
        monkeypatch.setattr(mgr, "_gate_precommand", slow_gate)
        t = asyncio.create_task(mgr.relaunch("tx", StartRequest()))
        await entered.wait()
        await proc.stop(operator=True)                   # the operator's Stop lands mid-precommand
        gate.set()
        await t
        assert calls == []                               # the relaunch did not proceed past it
    asyncio.run(scenario())


# ── #10: stop() during the launch's exec window ──────────────────────────────────────────────────

def test_stop_during_starting_kills_the_spawned_process(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "CTRL_DIR", tmp_path / "ctl")
    real_exec = asyncio.create_subprocess_exec
    gate, entered = asyncio.Event(), asyncio.Event()

    async def slow_exec(*a, **kw):
        entered.set()
        await gate.wait()
        return await real_exec(*a, **kw)
    monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", slow_exec)

    async def scenario():
        mgr, proc = _relaunchable(tmp_path)
        proc.config.env = {"PYTHONPATH": T.REPO_ROOT}
        launch = asyncio.create_task(proc.start())
        await entered.wait()
        assert proc.state == ProcessState.STARTING
        stopper = asyncio.create_task(proc.stop())
        await asyncio.sleep(0.1)
        assert not stopper.done()                        # waiting for the spawn to signal
        gate.set()
        with pytest.raises(RuntimeError):
            await launch                                 # refused to report a start
        await stopper
        assert proc.state == ProcessState.STOPPED
        assert proc._proc is not None and proc._proc.returncode is not None
    asyncio.run(scenario())


# ── #13 / #14: the watchdog never acts on a stale read ───────────────────────────────────────────

def test_scan_ignores_a_read_whose_process_exited_meanwhile(tmp_path):
    async def scenario():
        mgr, proc = _relaunchable(tmp_path)
        proc.state = ProcessState.RUNNING
        proc._proc = _FakeProc(alive=True)

        async def read_since(off, inode):
            proc.state = ProcessState.CRASHED           # the exit path took over during the read
            return "HEALTH state=faulted reason=x\n", 10, 1
        proc.log.read_since = read_since
        stops = []
        async def stop(*a, **kw):
            stops.append(kw)
        proc.stop = stop
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value and not proc._fault_alarmed and stops == []
    asyncio.run(scenario())


def test_scan_ignores_a_read_belonging_to_a_replaced_process(tmp_path):
    async def scenario():
        mgr, proc = _relaunchable(tmp_path)
        proc.state = ProcessState.RUNNING
        proc._proc = _FakeProc(alive=True)

        async def read_since(off, inode):
            proc._proc = _FakeProc(alive=True)          # a relaunch replaced the process
            return "vmcircbuf: no space\n", 10, 1
        proc.log.read_since = read_since
        stops = []
        async def stop(*a, **kw):
            stops.append(kw)
        proc.stop = stop
        await mgr._scan_task_health(proc)
        assert proc.health == TaskHealth.OK.value and stops == []    # the healthy relaunch survives
    asyncio.run(scenario())


# ── #19: healthy means TRANSMITTING when the script reports it ───────────────────────────────────

def test_healthy_settle_requires_the_transmitting_marker(tmp_path):
    async def scenario():
        mgr, proc = _relaunchable(tmp_path)
        (tmp_path / "tx.py").write_text(T.LIVE_SCRIPT + "\n# uses watch_flowgraph(tb, stop)\n")
        assert mgr.expects_tx_marker("tx") is True
        proc.state = ProcessState.RUNNING
        proc._proc = _FakeProc(alive=True)
        assert mgr.task_transmitting_confirmed("tx") is False        # warm-up is not healthy time
        async def read_since(off, inode):
            return TRANSMITTING_MARKER + "\n", 10, 1
        proc.log.read_since = read_since
        await mgr._scan_task_health(proc)
        assert proc.transmitting_at is not None
        assert mgr.task_transmitting_confirmed("tx") is True
        # a script WITHOUT the marker keeps the running-and-OK rule
        (tmp_path / "plain.py").write_text(T.LIVE_SCRIPT)
        mgr2 = pm.ProcessManager({"p": TaskConfig(name="p", command=["python3", str(tmp_path / "plain.py")],
                                                  working_dir=str(tmp_path))}, tmp_path, unit_id="u")
        mgr2._procs["p"].state = ProcessState.RUNNING
        assert mgr2.expects_tx_marker("p") is False and mgr2.task_transmitting_confirmed("p") is True
        # and the runner's _task_healthy consults it
        runner = SequenceRunner(mgr, "u", tmp_path / "s.json", tmp_path / "r.json", tmp_path)
        proc.transmitting_at = None
        assert runner._task_healthy("tx") is False
        proc.transmitting_at = "now"
        assert runner._task_healthy("tx") is True
    asyncio.run(scenario())


# ── #9 / #8: the boot pre-image and task launches never collide on the SDR ───────────────────────

def test_device_free_gate_holds_a_launch_while_the_probe_owns_the_sdr(tmp_path):
    async def scenario():
        mgr, proc = _relaunchable(tmp_path)
        calls = _stub_start(proc)
        mgr.device_free.clear()
        t = asyncio.create_task(mgr.start("tx"))
        await asyncio.sleep(0.2)
        assert calls == []                               # parked on the gate
        mgr.device_free.set()
        await t
        assert len(calls) == 1
    asyncio.run(scenario())


def test_preimage_holds_the_device_gate_and_zero_timeout_disables(tmp_path, monkeypatch):
    import agent.main as M
    mgr, proc = _relaunchable(tmp_path)
    monkeypatch.setattr(M, "_manager", mgr)
    monkeypatch.setattr(M.cfg, "PREIMAGE_ON_BOOT", True)
    seen = {}

    async def probe(timeout):
        seen["during"] = mgr.device_free.is_set()
        return "ok"
    monkeypatch.setattr(M.sysmon, "pre_image_sdr", probe)

    async def scenario():
        monkeypatch.setattr(M.cfg, "PREIMAGE_TIMEOUT_S", 5.0)
        await M._preimage_when_idle()
        assert seen == {"during": False} and mgr.device_free.is_set()   # held, then released
        seen.clear()
        monkeypatch.setattr(M.cfg, "PREIMAGE_TIMEOUT_S", 0.0)
        await M._preimage_when_idle()
        assert seen == {}                                # a zero timeout is a disable
    asyncio.run(scenario())


# ── #1: the GR vmcircbuf pin is a pref FILE GR actually reads ────────────────────────────────────

def test_gr_vmcircbuf_pin_writes_the_pref_file_and_the_snapshot_reads_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(pm._agentcfg, "TASK_HOME", str(home))
    monkeypatch.setattr(pm._agentcfg, "GR_VMCIRCBUF_FACTORY", "mmap_shm_open")
    pins = pm._launch_env_pins(tmp_path)
    name = "gr::vmcircbuf_mmap_shm_open_factory"
    for rel in (".config/gnuradio/prefs", ".gnuradio/prefs"):        # 3.10 and 3.8 locations
        assert (home / rel / "vmcircbuf_default_factory").read_text() == name
    assert pins["GR_PREFS_PATH"] == str(home / ".config" / "gnuradio")
    assert pins["HOME"] == str(home)
    assert sysmon._read_vmcircbuf_pref(str(home)) == name
    snap = sysmon.capture_fault_snapshot(None, task_dir=str(tmp_path))
    assert snap.vmcircbuf_backend_pref == name
    assert not any("no vmcircbuf pref file" in n for n in snap.notes)
    # without the file the snapshot says so (GR probed for itself → SysV suspect)
    monkeypatch.setattr(pm._agentcfg, "TASK_HOME", str(tmp_path / "empty"))
    snap2 = sysmon.capture_fault_snapshot(None, task_dir=str(tmp_path))
    assert snap2.vmcircbuf_backend_pref == "" and any("no vmcircbuf pref file" in n for n in snap2.notes)


# ── #23 / #25: uhd.log lifecycle; a zero poll interval disables the watchdog ─────────────────────

def test_uhd_log_rotates_per_run_and_is_pruned(tmp_path):
    lm = LogManager(tmp_path, "t")
    (lm.task_dir / "uhd.log").write_text("image load\n")
    lm.rotate_uhd()
    assert not (lm.task_dir / "uhd.log").exists() and list(lm.task_dir.glob("uhd_*.log"))
    assert lm.cleanup(keep_runs=0) >= 1 and not list(lm.task_dir.glob("uhd_*.log"))


def test_health_poll_zero_disables_the_watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(pm._agentcfg, "HEALTH_WATCH_ENABLED", True)
    monkeypatch.setattr(pm._agentcfg, "HEALTH_POLL_S", 0.0)

    async def scenario():
        mgr, _ = _relaunchable(tmp_path)
        await mgr.startup()
        assert mgr._health_task is None
        await mgr.shutdown()
    asyncio.run(scenario())


# ── #15 / #27 / #28: the ownership queries ───────────────────────────────────────────────────────

def test_ownership_queries_treat_faulted_pending_and_window_b_tasks_correctly(tmp_path, monkeypatch):
    mgr, runner = T._mk(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    faulted = T._faulted_auto_run(runner, now, rid="f")            # tx faulted, STOP skipped
    T0 = now - timedelta(seconds=5)
    armed = SequenceRun(id="a", sequence_id="s", sequence_name="later", state=SequenceState.ARMED,
                        on_air_at=T._iso(now + timedelta(seconds=60)), open_ended=True,
                        steps=[T._fire("start", 0.0, now + timedelta(seconds=60), fired=None, task="later")])
    holding = SequenceRun(id="h", sequence_id="s", sequence_name="hold", state=SequenceState.HOLDING,
                          on_air_at=T._iso(T0), open_ended=True, hold_aware=True,
                          steps=[T._fire("start", 0.0, T0, fired=T._iso(T0), task="live")],
                          window_b_steps=[SequenceStep(anchor="hold", offset_s=1.0, action=StepAction.START,
                                                       task_name="wb")])
    runner._runs.update({"a": armed, "h": holding})
    owned, claimed, pending = (runner._tasks_owned_by_active_runs(), runner.tasks_claimed_by_active_runs(),
                               runner.tasks_pending_launch_by_active_runs())
    assert "tx" not in owned                     # a faulted task is not on air because of its run (#15)
    assert "tx" in claimed and "tx" not in pending   # …but the run's policy owns its recovery
    assert "live" in owned and "live" in claimed and "live" not in pending
    assert {"later", "wb"} <= claimed and {"later", "wb"} <= pending    # window-B launch counts (#27)


def test_standalone_relaunch_waits_out_a_pending_launch_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(agentcfg, "AUTO_RESTART_ENABLED", True)

    async def scenario():
        mgr, proc = _relaunchable(tmp_path, delay=0.0)
        calls = _stub_start(proc)
        proc.state = ProcessState.CRASHED
        proc._proc = _FakeProc(code=1)
        polls = {"n": 0}
        def owned():
            polls["n"] += 1
            return {"tx"} if polls["n"] <= 2 else set()   # the claim clears after two polls
        proc._owned_query = owned
        proc._pending_query = lambda: {"tx"}
        await proc._maybe_auto_restart_standalone()
        assert len(calls) == 1                            # waited it out, then relaunched
        # a claim that is a run DRIVING the task stands down for good
        proc.state = ProcessState.CRASHED
        proc._owned_query = lambda: {"tx"}
        proc._pending_query = lambda: set()
        await proc._maybe_auto_restart_standalone()
        assert len(calls) == 1
    asyncio.run(scenario())


# ── #16 / #17 / #18 / #20 / #31 / #30: restart_run and the auto trigger edges ────────────────────

def test_refused_restart_does_not_kill_a_hand_started_task(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = T._faulted_auto_run(runner, now, policy="manual")
        stop = [s for s in run.steps if s.action == "stop"][0]
        stop.fire_at = T._iso(now - timedelta(seconds=2))          # off-air already past → resync refuses
        run.on_air_end = T._iso(now - timedelta(seconds=2))
        await mgr.start("tx")                                      # the operator brought it back by hand
        await asyncio.sleep(0.3)
        assert mgr.is_running("tx")
        with pytest.raises(ValueError):
            await runner.restart_run(run.id, RestartRequest(mode="resync"))
        assert mgr.is_running("tx") and run.fault                  # untouched, fault intact
        await mgr.stop("tx")
        # even with a valid plan, a task started by hand after the fault is refused, not killed
        stop.fire_at = T._iso(now + timedelta(seconds=10))
        run.on_air_end = T._iso(now + timedelta(seconds=10))
        await mgr.start("tx")
        await asyncio.sleep(0.3)
        with pytest.raises(ValueError, match="started by hand"):
            await runner.restart_run(run.id, RestartRequest(mode="resync"))
        assert mgr.is_running("tx")
        await mgr.shutdown()
    asyncio.run(scenario())


def test_restart_accepts_an_unfired_future_stop_after_a_holding_fault(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = T._faulted_auto_run(runner, now, policy="manual")
        stop = [s for s in run.steps if s.action == "stop"][0]
        stop.fired_actual = None                                   # appended un-skipped by proceed
        out = await runner.restart_run(run.id, RestartRequest(mode="resync"))
        assert out.fault == ""
        await mgr.shutdown()
    asyncio.run(scenario())


def test_restart_defers_when_the_schema_is_unreadable_and_the_gate_was_tuned(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = T._faulted_auto_run(runner, now)                     # tuned rf=on then power -70
        monkeypatch.setattr(runner, "_spec_of", lambda t: None)
        with pytest.raises(_RestartDeferred):
            await runner.restart_run(run.id, RestartRequest(mode="resync"))
        assert run.fault                                           # nothing mutated
        fired = T._recorder(monkeypatch, runner)
        await runner._service_auto_restart(now)                    # the auto path: quiet, no trip
        assert run.fault and run.id not in runner._auto_gaveup and run.auto_restart_count == 0
        assert not any(k == "sequence_rf_fault" for k, _ in fired)
    asyncio.run(scenario())


def test_budget_zero_refusal_trips_once_and_stops_retrying(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        monkeypatch.setattr(agentcfg, "AUTO_RESTART_BUDGET", 0)
        now = datetime.now(timezone.utc)
        run = T._faulted_auto_run(runner, now)
        stop = [s for s in run.steps if s.action == "stop"][0]
        stop.fire_at = T._iso(now - timedelta(seconds=2))          # resync must refuse
        run.on_air_end = T._iso(now - timedelta(seconds=2))
        fired = T._recorder(monkeypatch, runner)
        attempts = {"n": 0}
        orig = runner.restart_run
        async def counting(*a, **kw):
            attempts["n"] += 1
            return await orig(*a, **kw)
        monkeypatch.setattr(runner, "restart_run", counting)
        for _ in range(4):
            await runner._service_auto_restart(now)
        assert attempts["n"] == 1                                  # tripped once, never retried
        assert sum(1 for k, _ in fired if k == "sequence_rf_fault") == 1
    asyncio.run(scenario())


def test_faulted_hold_aware_run_does_not_park_into_holding(tmp_path, monkeypatch):
    async def scenario():
        mgr, runner = T._mk(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        run = T._faulted_auto_run(runner, now, policy="manual")
        run.hold_aware = True
        run.hold_at_offset_s = 1.0                                 # hold instant long past
        await runner._service_holds(now)
        assert run.state == SequenceState.RUNNING                 # still restartable
        run.fault = run.fault_task = run.fault_at = ""
        await runner._service_holds(now)
        assert run.state == SequenceState.HOLDING                 # a healthy run still parks
    asyncio.run(scenario())


def test_reconstruction_keeps_the_configured_command_for_an_empty_replace(tmp_path, monkeypatch):
    mgr, runner = T._mk(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    T0 = now - timedelta(seconds=10)
    run = SequenceRun(id="r", sequence_id="s", sequence_name="x", state=SequenceState.RUNNING,
                      on_air_at=T._iso(T0), steps=[
                          T._fire("start", 0.0, T0, fired=T._iso(T0), args=[], replace=True),
                          T._fire("tune", 1.0, T0 + timedelta(seconds=1), fired=T._iso(T0 + timedelta(seconds=1)),
                                  params={"rf": "on"})],
                      fault="x", fault_task="tx", fault_at=T._iso(now))
    fire = runner._relaunch_start_fire(run, "tx", now, runner._spec_of("tx"), include_skipped=True)
    assert fire.args[fire.args.index("--power") + 1] == "-90"      # the configured --power survives


# ── #24: the agent PlanItem round-trips a recovery override ──────────────────────────────────────

def test_plan_item_round_trips_recovery_fields():
    item = PlanItem(hostname="h", sequence_id="s", recovery_policy="auto", recovery_mode="replay")
    again = PlanItem(**item.model_dump())
    assert (again.recovery_policy, again.recovery_mode) == ("auto", "replay")
    assert PlanItem(hostname="h", sequence_id="s").recovery_policy == ""        # "" = inherit
